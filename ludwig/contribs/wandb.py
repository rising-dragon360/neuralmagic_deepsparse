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

import logging
import os
from datetime import datetime


logger = logging.getLogger(__name__)

wandb = None


class Wandb():
    """
    Class that defines the methods necessary to hook into process.
    """

    @staticmethod
    def import_call(argv, *args, **kwargs):
        """
        Enable Third-party support from wandb.ai
        Allows experiment tracking, visualization, and
        management.
        """
        try:
            global wandb
            import wandb
            return Wandb()
        except ImportError:
            logger.error(
                "Ignored --wandb: Please install wandb; see https://docs.wandb.com")
            return None

    def train_model(self, model, *args, **kwargs):
        logger.info("wandb.train_model() called...")
        config = model.hyperparameters.copy()
        del config["input_features"]
        del config["output_features"]
        wandb.config.update(config)

    def train_init(self, experiment_directory, experiment_name, model_name, resume, output_directory):
        logger.info("wandb.train_init() called...")
        wandb.init(project=os.getenv("WANDB_PROJECT", experiment_name), sync_tensorboard=True,
                   dir=output_directory)
        wandb.save(os.path.join(experiment_directory, "*"))

    def visualize_figure(self, fig):
        logger.info("wandb.visualize_figure() called...")
        wandb.log({"figure": fig})

    def predict_end(self, stats, *args, **kwargs):
        logger.info("wanbb.predict() called... %s" % stats)
        wandb.summary.update(dict(stats))
