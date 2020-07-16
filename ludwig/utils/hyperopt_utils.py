#! /usr/bin/env python
# coding=utf-8
# Copyright (c) 2019 Uber Technologies, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
import copy
import functools
import itertools
import logging
import math
import multiprocessing
import os
import random
import signal
from abc import ABC, abstractmethod
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Tuple

import numpy as np
import scipy.stats as ss

from bayesmark.space import JointSpace
from bayesmark.builtin_opt.pysot_optimizer import PySOTOptimizer
from ludwig.constants import COMBINED, EXECUTOR, LOSS, MAXIMIZE, MINIMIZE, STRATEGY, TEST, TRAINING, VALIDATION
from ludwig.data.postprocessing import postprocess
from ludwig.predict import predict, print_test_results, save_prediction_outputs, save_test_statistics
from ludwig.train import full_train
from ludwig.utils.defaults import default_random_seed
from ludwig.utils.misc import get_class_attributes, get_from_registry, set_default_value, set_default_values

logger = logging.getLogger(__name__)


class HyperoptStrategy(ABC):
    def __init__(self, goal: str, parameters: Dict[str, Any]) -> None:
        assert goal in [MINIMIZE, MAXIMIZE]
        self.goal = goal  # useful for Bayesian strategy
        self.parameters = parameters

    @abstractmethod
    def sample(self) -> Dict[str, Any]:
        # Yields a set of parameters names and their values.
        # Define `build_hyperopt_strategy` which would take paramters as inputs
        pass

    def sample_batch(self, batch_size: int = 1) -> List[Dict[str, Any]]:
        samples = []
        for _ in range(batch_size):
            try:
                samples.append(self.sample())
            except IndexError:
                # Logic: is samples is empty it means that we encountered
                # the IndexError the first time we called self.sample()
                # so we should raise the exception. If samples is not empty
                # we should just return it, even if it will contain
                # less samples than the specified batch_size.
                # This is fine as from now on finished() will return True.
                if not samples:
                    raise IndexError
        return samples

    @abstractmethod
    def update(self, sampled_parameters: Dict[str, Any], metric_score: float):
        # Given the results of previous computation, it updates
        # the strategy (not needed for stateless strategies like "grid"
        # and random, but will be needed by Bayesian)
        pass

    def update_batch(self, parameters_metric_tuples: Iterable[Tuple[Dict[str, Any], float]]):
        for (sampled_parameters, metric_score) in parameters_metric_tuples:
            self.update(sampled_parameters, metric_score)

    @abstractmethod
    def finished(self) -> bool:
        # Should return true when all samples have been sampled
        pass


class RandomStrategy(HyperoptStrategy):
    num_samples = 10

    def __init__(self, goal: str, parameters: Dict[str, Any], num_samples=10, **kwargs) -> None:
        HyperoptStrategy.__init__(self, goal, parameters)
        self.space = JointSpace(parameters)
        self.num_samples = num_samples
        self.samples = self._determine_samples()
        self.sampled_so_far = 0

    def _determine_samples(self):
        samples = []
        for _ in range(self.num_samples):
            bnds = self.space.get_bounds()
            x = bnds[:, 0] + (bnds[:, 1] - bnds[:, 0]) * np.random.rand(1, len(self.space.get_bounds()))
            sample = self.space.unwarp(x)[0]
            samples.append(sample)
        return samples

    def sample(self) -> Dict[str, Any]:
        if self.sampled_so_far >= len(self.samples):
            raise IndexError()
        sample = self.samples[self.sampled_so_far]
        self.sampled_so_far += 1
        return sample

    def update(self, sampled_parameters: Dict[str, Any], metric_score: float):
        pass

    def finished(self) -> bool:
        return self.sampled_so_far >= len(self.samples)


class PySOTStrategy(HyperoptStrategy):
    """pySOT: Surrogate optimization in Python.

    This is a wrapper around the pySOT package (https://github.com/dme65/pySOT):
        David Eriksson, David Bindel, Christine Shoemaker
        pySOT and POAP: An event-driven asynchronous framework for surrogate optimization
    """
    def __init__(self, goal: str, parameters: Dict[str, Any], num_samples=10, **kwargs) -> None:
        HyperoptStrategy.__init__(self, goal, parameters)
        self.pysot_optimizer = PySOTOptimizer(parameters)
        self.sampled_so_far = 0
        self.num_samples = num_samples

    def sample(self) -> Dict[str, Any]:
        """Suggest one new point to be evaluated."""
        sample = self.pysot_optimizer.suggest(n_suggestions=1)[0]
        self.sampled_so_far += 1
        return sample

    def update(self, sampled_parameters: Dict[str, Any], metric_score: float):
        self.pysot_optimizer.observe([sampled_parameters], [metric_score])

    def finished(self) -> bool:
        return self.sampled_so_far >= self.num_samples


class HyperoptExecutor(ABC):
    def __init__(self, hyperopt_strategy: HyperoptStrategy, output_feature: str, metric: str, split: str) -> None:
        self.hyperopt_strategy = hyperopt_strategy
        self.output_feature = output_feature
        self.metric = metric
        self.split = split

    def get_metric_score(self, eval_stats) -> float:
        return eval_stats[self.output_feature][self.metric]

    def sort_hyperopt_results(self, hyperopt_results):
        return sorted(
            hyperopt_results, key=lambda hp_res: hp_res["metric_score"], reverse=self.hyperopt_strategy.goal == MAXIMIZE
        )

    @abstractmethod
    def execute(
        self,
        model_definition,
        data_df=None,
        data_train_df=None,
        data_validation_df=None,
        data_test_df=None,
        data_csv=None,
        data_train_csv=None,
        data_validation_csv=None,
        data_test_csv=None,
        data_hdf5=None,
        data_train_hdf5=None,
        data_validation_hdf5=None,
        data_test_hdf5=None,
        train_set_metadata_json=None,
        experiment_name="hyperopt",
        model_name="run",
        model_load_path=None,
        model_resume_path=None,
        skip_save_training_description=False,
        skip_save_training_statistics=False,
        skip_save_model=False,
        skip_save_progress=False,
        skip_save_log=False,
        skip_save_processed_input=False,
        skip_save_unprocessed_output=False,
        skip_save_test_predictions=False,
        skip_save_test_statistics=False,
        output_directory="results",
        gpus=None,
        gpu_fraction=1.0,
        use_horovod=False,
        random_seed=default_random_seed,
        debug=False,
        **kwargs
    ):
        pass


class SerialExecutor(HyperoptExecutor):
    def __init__(
        self, hyperopt_strategy: HyperoptStrategy, output_feature: str, metric: str, split: str, **kwargs
    ) -> None:
        HyperoptExecutor.__init__(self, hyperopt_strategy, output_feature, metric, split)

    def execute(
        self,
        model_definition,
        data_df=None,
        data_train_df=None,
        data_validation_df=None,
        data_test_df=None,
        data_csv=None,
        data_train_csv=None,
        data_validation_csv=None,
        data_test_csv=None,
        data_hdf5=None,
        data_train_hdf5=None,
        data_validation_hdf5=None,
        data_test_hdf5=None,
        train_set_metadata_json=None,
        experiment_name="hyperopt",
        model_name="run",
        # model_load_path=None,
        # model_resume_path=None,
        skip_save_training_description=False,
        skip_save_training_statistics=False,
        skip_save_model=False,
        skip_save_progress=False,
        skip_save_log=False,
        skip_save_processed_input=False,
        skip_save_unprocessed_output=False,
        skip_save_test_predictions=False,
        skip_save_test_statistics=False,
        output_directory="results",
        gpus=None,
        gpu_fraction=1.0,
        use_horovod=False,
        random_seed=default_random_seed,
        debug=False,
        **kwargs
    ):
        hyperopt_results = []
        while not self.hyperopt_strategy.finished():
            sampled_parameters = self.hyperopt_strategy.sample_batch()
            metric_scores = []

            for parameters in sampled_parameters:
                modified_model_definition = substitute_parameters(copy.deepcopy(model_definition), parameters)

                train_stats, eval_stats = train_and_eval_on_split(
                    modified_model_definition,
                    eval_split=self.split,
                    data_df=data_df,
                    data_train_df=data_train_df,
                    data_validation_df=data_validation_df,
                    data_test_df=data_test_df,
                    data_csv=data_csv,
                    data_train_csv=data_train_csv,
                    data_validation_csv=data_validation_csv,
                    data_test_csv=data_test_csv,
                    data_hdf5=data_hdf5,
                    data_train_hdf5=data_train_hdf5,
                    data_validation_hdf5=data_validation_hdf5,
                    data_test_hdf5=data_test_hdf5,
                    train_set_metadata_json=train_set_metadata_json,
                    experiment_name=experiment_name,
                    model_name=model_name,
                    # model_load_path=model_load_path,
                    # model_resume_path=model_resume_path,
                    skip_save_training_description=skip_save_training_description,
                    skip_save_training_statistics=skip_save_training_statistics,
                    skip_save_model=skip_save_model,
                    skip_save_progress=skip_save_progress,
                    skip_save_log=skip_save_log,
                    skip_save_processed_input=skip_save_processed_input,
                    skip_save_unprocessed_output=skip_save_unprocessed_output,
                    skip_save_test_predictions=skip_save_test_predictions,
                    skip_save_test_statistics=skip_save_test_statistics,
                    output_directory=output_directory,
                    gpus=gpus,
                    gpu_fraction=gpu_fraction,
                    use_horovod=use_horovod,
                    random_seed=random_seed,
                    debug=debug,
                )
                metric_score = self.get_metric_score(eval_stats)
                metric_scores.append(metric_score)

                hyperopt_results.append(
                    {
                        "parameters": parameters,
                        "metric_score": metric_score,
                        "training_stats": train_stats,
                        "eval_stats": eval_stats,
                    }
                )

            self.hyperopt_strategy.update_batch(zip(sampled_parameters, metric_scores))

        hyperopt_results = self.sort_hyperopt_results(hyperopt_results)

        return hyperopt_results


class ParallelExecutor(HyperoptExecutor):
    num_workers = 2
    epsilon = 0.01

    def __init__(
        self,
        hyperopt_strategy: HyperoptStrategy,
        output_feature: str,
        metric: str,
        split: str,
        num_workers: int = 2,
        epsilon: int = 0.01,
        **kwargs
    ) -> None:
        HyperoptExecutor.__init__(self, hyperopt_strategy, output_feature, metric, split)
        self.num_workers = num_workers
        self.epsilon = epsilon
        self.queue = None

    @staticmethod
    def init_worker():
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    def _train_and_eval_model(self, hyperopt_dict):
        parameters = hyperopt_dict["parameters"]
        train_stats, eval_stats = train_and_eval_on_split(**hyperopt_dict)
        metric_score = self.get_metric_score(eval_stats)

        return {
            "parameters": parameters,
            "metric_score": metric_score,
            "training_stats": train_stats,
            "eval_stats": eval_stats,
        }

    def _train_and_eval_model_gpu(self, hyperopt_dict):
        gpu_id = self.queue.get()
        try:
            parameters = hyperopt_dict["parameters"]
            hyperopt_dict["gpus"] = gpu_id
            train_stats, eval_stats = train_and_eval_on_split(**hyperopt_dict)
            metric_score = self.get_metric_score(eval_stats)
        finally:
            self.queue.put(gpu_id)
        return {
            "parameters": parameters,
            "metric_score": metric_score,
            "training_stats": train_stats,
            "eval_stats": eval_stats,
        }

    def execute(
        self,
        model_definition,
        data_df=None,
        data_train_df=None,
        data_validation_df=None,
        data_test_df=None,
        data_csv=None,
        data_train_csv=None,
        data_validation_csv=None,
        data_test_csv=None,
        data_hdf5=None,
        data_train_hdf5=None,
        data_validation_hdf5=None,
        data_test_hdf5=None,
        train_set_metadata_json=None,
        experiment_name="hyperopt",
        model_name="run",
        # model_load_path=None,
        # model_resume_path=None,
        skip_save_training_description=False,
        skip_save_training_statistics=False,
        skip_save_model=False,
        skip_save_progress=False,
        skip_save_log=False,
        skip_save_processed_input=False,
        skip_save_unprocessed_output=False,
        skip_save_test_predictions=False,
        skip_save_test_statistics=False,
        output_directory="results",
        gpus=None,
        gpu_fraction=1.0,
        use_horovod=False,
        random_seed=default_random_seed,
        debug=False,
        **kwargs
    ):
        hyperopt_parameters = []

        if gpus is not None:

            num_available_cpus = multiprocessing.cpu_count()

            if self.num_workers > num_available_cpus:
                logger.warning(
                    "WARNING: Setting num_workers to less "
                    "or equal to number of available cpus: {} is suggested".format(num_available_cpus)
                )

            if isinstance(gpus, int):
                gpus = str(gpus)
            gpus = gpus.strip()
            gpu_ids = gpus.split(",")
            total_gpus = len(gpu_ids)

            if total_gpus < self.num_workers:
                fraction = (total_gpus / self.num_workers) - self.epsilon
                if fraction < gpu_fraction:
                    if fraction > 0.5:
                        if gpu_fraction != 1:
                            logger.warning(
                                "WARNING: Setting gpu_fraction to 1 as the gpus "
                                "would be underutilized for the parallel processes."
                            )
                        gpu_fraction = 1
                    else:
                        logger.warning(
                            "WARNING: Setting gpu_fraction to {} "
                            "as the available gpus is {} and the num of workers "
                            "selected is {}".format(fraction, total_gpus, self.num_workers)
                        )
                        gpu_fraction = fraction
                else:
                    logger.warning(
                        "WARNING: gpu_fraction could be increased to {} "
                        "as the available gpus is {} and the num of workers "
                        "being set is {}".format(fraction, total_gpus, self.num_workers)
                    )

            process_per_gpu = int(1 / gpu_fraction)

            manager = multiprocessing.Manager()
            self.queue = manager.Queue()

            for gpu_id in gpu_ids:
                for _ in range(process_per_gpu):
                    self.queue.put(gpu_id)

        pool = multiprocessing.Pool(self.num_workers, ParallelExecutor.init_worker)
        hyperopt_results = []
        while not self.hyperopt_strategy.finished():
            sampled_parameters = self.hyperopt_strategy.sample_batch()

            for parameters in sampled_parameters:
                modified_model_definition = substitute_parameters(copy.deepcopy(model_definition), parameters)

                hyperopt_parameters.append(
                    {
                        "parameters": parameters,
                        "model_definition": modified_model_definition,
                        "eval_split": self.split,
                        "data_df": data_df,
                        "data_train_df": data_train_df,
                        "data_validation_df": data_validation_df,
                        "data_test_df": data_test_df,
                        "data_csv": data_csv,
                        "data_train_csv": data_train_csv,
                        "data_validation_csv": data_validation_csv,
                        "data_test_csv": data_test_csv,
                        "data_hdf5": data_hdf5,
                        "data_train_hdf5": data_train_hdf5,
                        "data_validation_hdf5": data_validation_hdf5,
                        "data_test_hdf5": data_test_hdf5,
                        "train_set_metadata_json": train_set_metadata_json,
                        "experiment_name": experiment_name,
                        "model_name": model_name,
                        # model_load_path:model_load_path,
                        # model_resume_path:model_resume_path,
                        "skip_save_training_description": skip_save_training_description,
                        "skip_save_training_statistics": skip_save_training_statistics,
                        "skip_save_model": skip_save_model,
                        "skip_save_progress": skip_save_progress,
                        "skip_save_log": skip_save_log,
                        "skip_save_processed_input": skip_save_processed_input,
                        "skip_save_unprocessed_output": skip_save_unprocessed_output,
                        "skip_save_test_predictions": skip_save_test_predictions,
                        "skip_save_test_statistics": skip_save_test_statistics,
                        "output_directory": output_directory,
                        "gpus": gpus,
                        "gpu_fraction": gpu_fraction,
                        "use_horovod": use_horovod,
                        "random_seed": random_seed,
                        "debug": debug,
                    }
                )

            if gpus is not None:
                batch_results = pool.map(self._train_and_eval_model_gpu, hyperopt_parameters)
            else:
                batch_results = pool.map(self._train_and_eval_model, hyperopt_parameters)

            self.hyperopt_strategy.update_batch(
                (result["parameters"], result["metric_score"]) for result in batch_results
            )

            hyperopt_results.extend(batch_results)

        hyperopt_results = self.sort_hyperopt_results(hyperopt_results)
        return hyperopt_results


class FiberExecutor(HyperoptExecutor):
    num_workers = 2
    fiber_backend = "local"

    def __init__(
        self,
        hyperopt_strategy: HyperoptStrategy,
        output_feature: str,
        metric: str,
        split: str,
        num_workers: int = 2,
        num_cpus_per_worker: int = -1,
        num_gpus_per_worker: int = -1,
        fiber_backend: str = "local",
        **kwargs
    ) -> None:
        import fiber

        HyperoptExecutor.__init__(self, hyperopt_strategy, output_feature, metric, split)

        fiber.init(backend=fiber_backend)
        self.fiber_meta = fiber.meta

        self.num_cpus_per_worker = num_cpus_per_worker
        self.num_gpus_per_worker = num_gpus_per_worker

        self.resource_limits = {}
        if num_cpus_per_worker != -1:
            self.resource_limits["cpu"] = num_cpus_per_worker

        if num_gpus_per_worker != -1:
            self.resource_limits["gpu"] = num_gpus_per_worker

        self.num_workers = num_workers
        self.pool = fiber.Pool(num_workers)

    def execute(
        self,
        model_definition,
        data_df=None,
        data_train_df=None,
        data_validation_df=None,
        data_test_df=None,
        data_csv=None,
        data_train_csv=None,
        data_validation_csv=None,
        data_test_csv=None,
        data_hdf5=None,
        data_train_hdf5=None,
        data_validation_hdf5=None,
        data_test_hdf5=None,
        train_set_metadata_json=None,
        experiment_name="hyperopt",
        model_name="run",
        # model_load_path=None,
        # model_resume_path=None,
        skip_save_training_description=False,
        skip_save_training_statistics=False,
        skip_save_model=False,
        skip_save_progress=False,
        skip_save_log=False,
        skip_save_processed_input=False,
        skip_save_unprocessed_output=False,
        skip_save_test_predictions=False,
        skip_save_test_statistics=False,
        output_directory="results",
        gpus=None,
        gpu_fraction=1.0,
        use_horovod=False,
        random_seed=default_random_seed,
        debug=False,
        **kwargs
    ):
        train_func = functools.partial(
            train_and_eval_on_split,
            eval_split=self.split,
            data_df=data_df,
            data_train_df=data_train_df,
            data_validation_df=data_validation_df,
            data_test_df=data_test_df,
            data_csv=data_csv,
            data_train_csv=data_train_csv,
            data_validation_csv=data_validation_csv,
            data_test_csv=data_test_csv,
            data_hdf5=data_hdf5,
            data_train_hdf5=data_train_hdf5,
            data_validation_hdf5=data_validation_hdf5,
            data_test_hdf5=data_test_hdf5,
            train_set_metadata_json=train_set_metadata_json,
            experiment_name=experiment_name,
            model_name=model_name,
            # model_load_path=model_load_path,
            # model_resume_path=model_resume_path,
            skip_save_training_description=skip_save_training_description,
            skip_save_training_statistics=skip_save_training_statistics,
            skip_save_model=skip_save_model,
            skip_save_progress=skip_save_progress,
            skip_save_log=skip_save_log,
            skip_save_processed_input=skip_save_processed_input,
            skip_save_unprocessed_output=skip_save_unprocessed_output,
            skip_save_test_predictions=skip_save_test_predictions,
            skip_save_test_statistics=skip_save_test_statistics,
            output_directory=output_directory,
            gpus=gpus,
            gpu_fraction=gpu_fraction,
            use_horovod=use_horovod,
            random_seed=random_seed,
            debug=debug,
        )

        if self.resource_limits:
            train_func = self.fiber_meta(**self.resource_limits)(train_func)

        hyperopt_results = []
        while not self.hyperopt_strategy.finished():
            sampled_parameters = self.hyperopt_strategy.sample_batch()
            metric_scores = []

            stats_batch = self.pool.map(
                train_func,
                [
                    substitute_parameters(copy.deepcopy(model_definition), parameters)
                    for parameters in sampled_parameters
                ],
            )

            for stats, parameters in zip(stats_batch, sampled_parameters):
                train_stats, eval_stats = stats
                metric_score = self.get_metric_score(eval_stats)
                metric_scores.append(metric_score)

                hyperopt_results.append(
                    {
                        "parameters": parameters,
                        "metric_score": metric_score,
                        "training_stats": train_stats,
                        "eval_stats": eval_stats,
                    }
                )

            self.hyperopt_strategy.update_batch(zip(sampled_parameters, metric_scores))

        hyperopt_results = self.sort_hyperopt_results(hyperopt_results)

        return hyperopt_results


def get_build_hyperopt_strategy(strategy_type):
    return get_from_registry(strategy_type, strategy_registry)


def get_build_hyperopt_executor(executor_type):
    return get_from_registry(executor_type, executor_registry)


strategy_registry = {
    "random": RandomStrategy,
    "pysot": PySOTStrategy,
}

executor_registry = {
    "serial": SerialExecutor,
    "parallel": ParallelExecutor,
    "fiber": FiberExecutor,
}


def update_hyperopt_params_with_defaults(hyperopt_params):
    set_default_value(hyperopt_params, STRATEGY, {})
    set_default_value(hyperopt_params, EXECUTOR, {})
    set_default_value(hyperopt_params, "split", VALIDATION)
    set_default_value(hyperopt_params, "output_feature", COMBINED)
    set_default_value(hyperopt_params, "metric", LOSS)
    set_default_value(hyperopt_params, "goal", MINIMIZE)

    set_default_values(hyperopt_params[STRATEGY], {"type": "random"})

    strategy = get_from_registry(hyperopt_params[STRATEGY]["type"], strategy_registry)
    strategy_defaults = {k: v for k, v in strategy.__dict__.items() if k in get_class_attributes(strategy)}
    set_default_values(
        hyperopt_params[STRATEGY], strategy_defaults,
    )

    set_default_values(hyperopt_params[EXECUTOR], {"type": "serial"})

    executor = get_from_registry(hyperopt_params[EXECUTOR]["type"], executor_registry)
    executor_defaults = {k: v for k, v in executor.__dict__.items() if k in get_class_attributes(executor)}
    set_default_values(
        hyperopt_params[EXECUTOR], executor_defaults,
    )


def set_values(model_dict, name, parameters_dict):
    if name in parameters_dict:
        params = parameters_dict[name]
        for key, value in params.items():
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    model_dict[key][sub_key] = sub_value
            else:
                model_dict[key] = value


def get_parameters_dict(parameters):
    parameters_dict = {}
    for name, value in parameters.items():
        curr_dict = parameters_dict
        name_list = name.split(".")
        for i, name_elem in enumerate(name_list):
            if i == len(name_list) - 1:
                curr_dict[name_elem] = value
            else:
                name_dict = curr_dict.get(name_elem, {})
                curr_dict[name_elem] = name_dict
                curr_dict = name_dict
    return parameters_dict


def substitute_parameters(model_definition, parameters):
    parameters_dict = get_parameters_dict(parameters)
    for input_feature in model_definition["input_features"]:
        set_values(input_feature, input_feature["name"], parameters_dict)
    for output_feature in model_definition["output_features"]:
        set_values(output_feature, output_feature["name"], parameters_dict)
    set_values(model_definition["combiner"], "combiner", parameters_dict)
    set_values(model_definition["training"], "training", parameters_dict)
    set_values(model_definition["preprocessing"], "preprocessing", parameters_dict)
    return model_definition


# TODo this is duplicate code from experiment,
#  reorganize experiment to avoid having to do this
def train_and_eval_on_split(
    model_definition,
    eval_split=VALIDATION,
    data_df=None,
    data_train_df=None,
    data_validation_df=None,
    data_test_df=None,
    data_csv=None,
    data_train_csv=None,
    data_validation_csv=None,
    data_test_csv=None,
    data_hdf5=None,
    data_train_hdf5=None,
    data_validation_hdf5=None,
    data_test_hdf5=None,
    train_set_metadata_json=None,
    experiment_name="hyperopt",
    model_name="run",
    # model_load_path=None,
    # model_resume_path=None,
    skip_save_training_description=False,
    skip_save_training_statistics=False,
    skip_save_model=False,
    skip_save_progress=False,
    skip_save_log=False,
    skip_save_processed_input=False,
    skip_save_unprocessed_output=False,
    skip_save_test_predictions=False,
    skip_save_test_statistics=False,
    output_directory="results",
    gpus=None,
    gpu_fraction=1.0,
    use_horovod=False,
    random_seed=default_random_seed,
    debug=False,
    **kwargs
):
    # Collect training and validation losses and measures
    # & append it to `results`
    # ludwig_model = LudwigModel(modified_model_definition)
    (model, preprocessed_data, experiment_dir_name, train_stats, model_definition) = full_train(
        model_definition=model_definition,
        data_df=data_df,
        data_train_df=data_train_df,
        data_validation_df=data_validation_df,
        data_test_df=data_test_df,
        data_csv=data_csv,
        data_train_csv=data_train_csv,
        data_validation_csv=data_validation_csv,
        data_test_csv=data_test_csv,
        data_hdf5=data_hdf5,
        data_train_hdf5=data_train_hdf5,
        data_validation_hdf5=data_validation_hdf5,
        data_test_hdf5=data_test_hdf5,
        train_set_metadata_json=train_set_metadata_json,
        experiment_name=experiment_name,
        model_name=model_name,
        # model_load_path=model_load_path,
        # model_resume_path=model_resume_path,
        skip_save_training_description=skip_save_training_description,
        skip_save_training_statistics=skip_save_training_statistics,
        skip_save_model=skip_save_model,
        skip_save_progress=skip_save_progress,
        skip_save_log=skip_save_log,
        skip_save_processed_input=skip_save_processed_input,
        output_directory=output_directory,
        gpus=gpus,
        gpu_fraction=gpu_fraction,
        use_horovod=use_horovod,
        random_seed=random_seed,
        debug=debug,
    )
    (training_set, validation_set, test_set, train_set_metadata) = preprocessed_data
    if model_definition[TRAINING]["eval_batch_size"] > 0:
        batch_size = model_definition[TRAINING]["eval_batch_size"]
    else:
        batch_size = model_definition[TRAINING]["batch_size"]

    eval_set = validation_set
    if eval_split == TRAINING:
        eval_set = training_set
    elif eval_split == VALIDATION:
        eval_set = validation_set
    elif eval_split == TEST:
        eval_set = test_set

    test_results = predict(
        eval_set,
        train_set_metadata,
        model,
        model_definition,
        batch_size,
        evaluate_performance=True,
        gpus=gpus,
        gpu_fraction=gpu_fraction,
        debug=debug,
    )
    if not (skip_save_unprocessed_output and skip_save_test_predictions and skip_save_test_statistics):
        if not os.path.exists(experiment_dir_name):
            os.makedirs(experiment_dir_name)

    # postprocess
    postprocessed_output = postprocess(
        test_results,
        model_definition["output_features"],
        train_set_metadata,
        experiment_dir_name,
        skip_save_unprocessed_output,
    )

    print_test_results(test_results)
    if not skip_save_test_predictions:
        save_prediction_outputs(postprocessed_output, experiment_dir_name)
    if not skip_save_test_statistics:
        save_test_statistics(test_results, experiment_dir_name)
    return train_stats, test_results
