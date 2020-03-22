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

import numpy as np
import tensorflow.compat.v1 as tf

from ludwig.constants import *
from ludwig.models.modules.loss_modules import binary_weighted_cross_entropy_with_logits
from ludwig.utils.tf_utils import to_sparse

metrics = {ACCURACY, TOKEN_ACCURACY, HITS_AT_K, R2, JACCARD, EDIT_DISTANCE,
           MEAN_SQUARED_ERROR, MEAN_ABSOLUTE_ERROR,
           PERPLEXITY}

max_metrics = {ACCURACY, TOKEN_ACCURACY, HITS_AT_K, R2, JACCARD}
min_metrics = {EDIT_DISTANCE, MEAN_SQUARED_ERROR, MEAN_ABSOLUTE_ERROR, LOSS,
               PERPLEXITY}


#
# Custom classes to support Tensorflow 2
#
class R2Score(tf.keras.metrics.Metric):
    # custom tf.keras.metrics class to compute r2 score from batches
    # See for additional info:
    #   https://www.tensorflow.org/api_docs/python/tf/keras/metrics/Metric

    # todo tf2 - convert to tensors?

    def __init__(self, name='r2_score'):
        super(R2Score, self).__init__(name=name)
        self._reset_states()

    def _reset_states(self):
        self.sum_y = 0.0
        self.sum_y_squared = 0.0
        self.sum_y_hat = 0.0
        self.sum_y_hat_squared = 0.0
        self.sum_y_y_hat = 0.0
        self.N = 0

    def reset_states(self):
        self._reset_states()

    def update_state(self, y, y_hat):
        self.sum_y += np.sum(y)
        self.sum_y_squared += np.sum(y**2)
        self.sum_y_hat += np.sum(y_hat)
        self.sum_y_hat_squared += np.sum(y_hat**2)
        self.sum_y_y_hat += np.sum(y * y_hat)
        self.N += y.shape[0]

    def result(self):
        y_bar = self.sum_y / self.N
        tot_ss = self.sum_y_squared - 2.0 * y_bar * self.sum_y \
                 + self.N * y_bar ** 2
        res_ss = self.sum_y_squared - 2.0 * self.sum_y_y_hat \
                 + self.sum_y_hat_squared
        return 1.0 - res_ss / tot_ss


class ErrorScore(tf.keras.metrics.Metric):
    # See for additional info:
    #   https://www.tensorflow.org/api_docs/python/tf/keras/metrics/Metric

    # todo tf2 - convert to tensors?

    def __init__(self, name='error_score'):
        super(ErrorScore, self).__init__(name=name)
        self._reset_states()

    def _reset_states(self):
        self.sum_error = 0.0
        self.N = 0

    def reset_states(self):
        self._reset_states()

    def update_state(self, y, y_hat):
        self.sum_error += np.sum(y - y_hat)
        self.N += y.shape[0]

    def result(self):
        return self.sum_error / self.N


class BWCEWLScore(tf.keras.metrics.Metric):
    # Binary Weighted Cross Entropy Weighted Logits Score Metric
    # See for additional info:
    #   https://www.tensorflow.org/api_docs/python/tf/keras/metrics/Metric

    # todo tf2 - convert to tensors?

    def __init__(self, name='error_score'):
        super(BWCEWLScore, self).__init__(name=name)
        self._reset_states()

    def _reset_states(self):
        self.sum_loss = 0.0
        self.N = 0

    def reset_states(self):
        self._reset_states()

    def update_state(self, y, y_hat, **kwargs):
        loss = binary_weighted_cross_entropy_with_logits(
            y,
            y_hat,
            **kwargs
        )
        self.sum_loss += np.sum(loss)
        self.N += y.shape[0]

    def result(self):
        return self.sum_loss / self.N

# end of custom classes


def get_improved_fun(metric):
    if metric in min_metrics:
        return lambda x, y: x < y
    else:
        return lambda x, y: x > y


def get_initial_validation_value(metric):
    if metric in min_metrics:
        return float('inf')
    else:
        return float('-inf')


def get_best_function(metric):
    if metric in min_metrics:
        return min
    else:
        return max


def accuracy(targets, predictions, output_feature_name):
    correct_predictions = tf.equal(predictions, targets,
                                   name='correct_predictions_{}'.format(
                                       output_feature_name))
    accuracy = tf.reduce_mean(
        tf.cast(correct_predictions, tf.float32),
        name='accuracy_{}'.format(output_feature_name))
    return accuracy, correct_predictions


def masked_accuracy(targets, predictions, sequence_lengths,
                    output_feature_name):
    truncated_predictions = predictions[:, :targets.shape[1]]
    paddings = tf.stack([[0, 0], [0, tf.shape(targets)[1] -
                                  tf.shape(
                                      truncated_predictions)[1]]])
    padded_truncated_predictions = tf.pad(truncated_predictions,
                                          paddings,
                                          name='ptp')

    correct_predictions = tf.equal(padded_truncated_predictions,
                                   targets,
                                   name='overall_correct_predictions_{}'.format(
                                       output_feature_name))

    mask = tf.sequence_mask(sequence_lengths,
                            maxlen=correct_predictions.shape[1],
                            dtype=tf.int32)

    filtered_out, masked_correct_predictions = tf.dynamic_partition(
        correct_predictions, mask, 2)
    token_accuracy = tf.reduce_mean(
        tf.cast(masked_correct_predictions, tf.float32),
        name='token_accuracy_{}'.format(output_feature_name))

    one_masked_correct_prediction = 1.0 - tf.cast(mask,
                                                  tf.float32) + (
                                            tf.cast(mask,
                                                    tf.float32) * tf.cast(
                                        correct_predictions,
                                        tf.float32))
    rowwise_correct_predictions = tf.reduce_prod(
        one_masked_correct_prediction,
        axis=-1,
        name='rowwise_correct_predictions_{}'.format(
            output_feature_name))
    rowwise_accuracy = tf.reduce_mean(rowwise_correct_predictions,
                                      name='rowwise_accuracy_{}'.format(
                                          output_feature_name))

    return token_accuracy, masked_correct_predictions, rowwise_accuracy, rowwise_correct_predictions


def hits_at_k(targets, predictions_logits, top_k, output_feature_name):
    with tf.device('/cpu:0'):
        hits_at_k = tf.nn.in_top_k(predictions_logits, targets, top_k,
                                   name='hits_at_k_{}'.format(
                                       output_feature_name))
        mean_hits_at_k = tf.reduce_mean(tf.cast(hits_at_k, tf.float32),
                                        name='mean_hits_at_k_{}'.format(
                                            output_feature_name))
    return hits_at_k, mean_hits_at_k


def edit_distance(targets, target_seq_length, predictions_sequence,
                  predictions_seq_length, output_feature_name):
    predicts = to_sparse(predictions_sequence,
                         predictions_seq_length,
                         tf.shape(predictions_sequence)[1])
    labels = to_sparse(targets,
                       target_seq_length,
                       tf.shape(targets)[1])
    edit_distance = tf.edit_distance(predicts, labels,
                                     name='edit_distance_{}'.format(
                                         output_feature_name))
    mean_edit_distance = tf.reduce_mean(edit_distance,
                                        name='mean_edit_distance_{}'.format(
                                            output_feature_name))
    return edit_distance, mean_edit_distance


def perplexity(cross_entropy_loss):
    # This seem weird but is correct:
    # we are returning the cross entropy loss as it will be later summed,
    # divided by the size of the dataset and finally exponentiated,
    # because perplexity has a avg_exp aggregation strategy
    # in the output config in SequenceOutputFeature.
    # This implies that in Model update_output_stats_batch()
    # the values read from the perplexity node will be summed
    # and in Model update_output_stats() they will be divided
    # by the set size first and exponentiated.
    return cross_entropy_loss


def error(targets, predictions, output_feature_name):
    # return tf.get_variable('error_{}'.format(output_feature_name), initializer=tf.subtract(targets, predictions))
    return tf.subtract(targets, predictions,
                       name='error_{}'.format(output_feature_name))


def absolute_error(targets, predictions, output_feature_name):
    # error = tf.get_variable('error_{}'.format(output_feature_name), initializer=tf.subtract(targets, predictions))
    error = tf.subtract(targets, predictions)
    return tf.abs(error, name='absolute_error_{}'.format(output_feature_name))


def squared_error(targets, predictions, output_feature_name):
    # error = tf.get_variable('error_{}'.format(output_feature_name), initializer=tf.subtract(targets, predictions))
    error = tf.subtract(targets, predictions)
    return tf.pow(error, 2, name='squared_error_{}'.format(output_feature_name))


def r2(targets, predictions, output_feature_name):
    y_bar = tf.reduce_mean(targets)
    tot_ss = tf.reduce_sum(tf.pow(targets - y_bar, 2))
    res_ss = tf.reduce_sum(tf.pow(targets - predictions, 2))
    r2 = tf.subtract(1., res_ss / tot_ss,
                     name='r2_{}'.format(output_feature_name))
    return r2
