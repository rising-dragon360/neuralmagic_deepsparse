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
import logging
import os

import numpy as np
import tensorflow as tf
from tensorflow.keras.layers import average

from ludwig.constants import *
from ludwig.features.base_feature import BaseFeature
from ludwig.features.base_feature import InputFeature
from ludwig.features.base_feature import OutputFeature
from ludwig.models.modules.loss_modules import SampledSoftmaxCrossEntropyLoss
from ludwig.models.modules.loss_modules import SequenceLoss
from ludwig.models.modules.metric_modules import EditDistanceMetric
from ludwig.models.modules.metric_modules import PerplexityMetric
from ludwig.models.modules.metric_modules import SequenceLastAccuracyMetric
from ludwig.models.modules.metric_modules import SequenceLossMetric
from ludwig.models.modules.metric_modules import TokenAccuracyMetric
from ludwig.models.modules.reduction_modules import reduce_sequence
from ludwig.models.modules.sequence_decoders import SequenceGeneratorDecoder
from ludwig.models.modules.sequence_decoders import SequenceTaggerDecoder
from ludwig.models.modules.sequence_encoders import BERT
from ludwig.models.modules.sequence_encoders import ParallelCNN
from ludwig.models.modules.sequence_encoders import SequenceEmbedEncoder
from ludwig.models.modules.sequence_encoders import SequencePassthroughEncoder
from ludwig.models.modules.sequence_encoders import StackedCNN
from ludwig.models.modules.sequence_encoders import StackedCNNRNN
from ludwig.models.modules.sequence_encoders import StackedParallelCNN
from ludwig.models.modules.sequence_encoders import StackedRNN
from ludwig.utils.math_utils import softmax
from ludwig.utils.metrics_utils import ConfusionMatrix
from ludwig.utils.misc import set_default_value
from ludwig.utils.strings_utils import PADDING_SYMBOL
from ludwig.utils.strings_utils import UNKNOWN_SYMBOL
from ludwig.utils.strings_utils import build_sequence_matrix
from ludwig.utils.strings_utils import create_vocabulary
from ludwig.utils.tf_utils import sequence_length_2D

logger = logging.getLogger(__name__)


class SequenceBaseFeature(BaseFeature):
    type = SEQUENCE

    preprocessing_defaults = {
        'sequence_length_limit': 256,
        'most_common': 20000,
        'padding_symbol': PADDING_SYMBOL,
        'unknown_symbol': UNKNOWN_SYMBOL,
        'padding': 'right',
        'tokenizer': 'space',
        'lowercase': False,
        'vocab_file': None,
        'missing_value_strategy': FILL_WITH_CONST,
        'fill_value': ''
    }

    def __init__(self, feature):
        super().__init__(feature)

    @staticmethod
    def get_feature_meta(column, preprocessing_parameters):
        idx2str, str2idx, str2freq, max_length = create_vocabulary(
            column, preprocessing_parameters['tokenizer'],
            lowercase=preprocessing_parameters['lowercase'],
            num_most_frequent=preprocessing_parameters['most_common'],
            vocab_file=preprocessing_parameters['vocab_file'],
            unknown_symbol=preprocessing_parameters['unknown_symbol'],
            padding_symbol=preprocessing_parameters['padding_symbol'],
        )
        max_length = min(
            preprocessing_parameters['sequence_length_limit'],
            max_length
        )
        return {
            'idx2str': idx2str,
            'str2idx': str2idx,
            'str2freq': str2freq,
            'vocab_size': len(idx2str),
            'max_sequence_length': max_length
        }

    @staticmethod
    def feature_data(column, metadata, preprocessing_parameters):
        sequence_data = build_sequence_matrix(
            sequences=column,
            inverse_vocabulary=metadata['str2idx'],
            tokenizer_type=preprocessing_parameters['tokenizer'],
            length_limit=metadata['max_sequence_length'],
            padding_symbol=preprocessing_parameters['padding_symbol'],
            padding=preprocessing_parameters['padding'],
            unknown_symbol=preprocessing_parameters['unknown_symbol'],
            lowercase=preprocessing_parameters['lowercase'],
            tokenizer_vocab_file=preprocessing_parameters[
                'vocab_file'
            ],
        )
        return sequence_data

    @staticmethod
    def add_feature_data(
            feature,
            dataset_df,
            data,
            metadata,
            preprocessing_parameters
    ):
        sequence_data = SequenceInputFeature.feature_data(
            dataset_df[feature['name']].astype(str),
            metadata[feature['name']], preprocessing_parameters)
        data[feature['name']] = sequence_data


class SequenceInputFeature(SequenceBaseFeature, InputFeature):
    encoder = 'embed'

    def __init__(self, feature, encoder_obj=None):
        SequenceBaseFeature.__init__(self, feature)
        InputFeature.__init__(self)
        self.overwrite_defaults(feature)
        if encoder_obj:
            self.encoder_obj = encoder_obj
        else:
            self.encoder_obj = self.initialize_encoder(feature)

    def call(self, inputs, training=None, mask=None):
        assert isinstance(inputs, tf.Tensor)
        assert inputs.dtype == tf.int8 or inputs.dtype == tf.int16 or \
               inputs.dtype == tf.int32 or inputs.dtype == tf.int64
        assert len(inputs.shape) == 2

        inputs_exp = tf.cast(inputs, dtype=tf.int32)
        encoder_output = self.encoder_obj(
            inputs_exp, training=training, mask=mask
        )

        return encoder_output

    @staticmethod
    def update_model_definition_with_metadata(
            input_feature,
            feature_metadata,
            *args,
            **kwargs
    ):
        input_feature['vocab'] = feature_metadata['idx2str']
        input_feature['length'] = feature_metadata['max_sequence_length']

    @staticmethod
    def populate_defaults(input_feature):
        set_default_value(input_feature, TIED, None)
        set_default_value(input_feature, 'encoder', 'parallel_cnn')

    encoder_registry = {
        'stacked_cnn': StackedCNN,
        'parallel_cnn': ParallelCNN,
        'stacked_parallel_cnn': StackedParallelCNN,
        'rnn': StackedRNN,
        'cnnrnn': StackedCNNRNN,
        'embed': SequenceEmbedEncoder,
        'bert': BERT,
        'passthrough': SequencePassthroughEncoder,
        'null': SequencePassthroughEncoder,
        'none': SequencePassthroughEncoder,
        'None': SequencePassthroughEncoder,
        None: SequencePassthroughEncoder
    }


class SequenceOutputFeature(SequenceBaseFeature, OutputFeature):
    decoder = 'tagger'
    loss = {'type': SOFTMAX_CROSS_ENTROPY}

    def __init__(self, feature):
        super().__init__(feature)
        self.type = SEQUENCE

        self.decoder = 'generator'
        self.max_sequence_length = 0
        self.loss = {
            'type': SOFTMAX_CROSS_ENTROPY,
            'sampler': None,
            'negative_samples': 0,
            'distortion': 1,
            'labels_smoothing': 0,
            'class_weights': 1,
            'robust_lambda': 0,
            'confidence_penalty': 0,
            'class_similarities_temperature': 0,
            'weight': 1
        }
        self.num_classes = 0

        self.overwrite_defaults(feature)

        self.decoder_obj = self.initialize_decoder(feature)

        self._setup_loss()
        self._setup_metrics()

    def _setup_loss(self):
        if self.loss['type'] == 'softmax_cross_entropy':
            self.train_loss_function = SequenceLoss()
        elif self.loss['type'] == 'sampled_softmax_cross_entropy':
            self.train_loss_function = SampledSoftmaxCrossEntropyLoss(
                decoder_obj=self.decoder_obj,
                num_classes=self.num_classes,
                feature_loss=self.loss,
                name='train_loss'
            )
        else:
            raise ValueError(
                "Loss type {} is not supported. Valid values are "
                "'softmax_cross_entropy' or "
                "'sampled_softmax_cross_entropy'".format(self.loss['type'])
            )

        self.eval_loss_function = SequenceLossMetric()

    def _setup_metrics(self):
        self.metric_functions[LOSS] = self.eval_loss_function
        self.metric_functions[TOKEN_ACCURACY] = TokenAccuracyMetric()
        self.metric_functions[LAST_ACCURACY] = SequenceLastAccuracyMetric()
        self.metric_functions[PERPLEXITY] = PerplexityMetric()
        self.metric_functions[EDIT_DISTANCE] = EditDistanceMetric()

    # over ride super class OutputFeature.update_metrics() method
    def update_metrics(self, targets, predictions):
        for metric, metric_fn in self.metric_functions.items():
            if metric == LOSS or metric == PERPLEXITY:
                metric_fn.update_state(targets, predictions)
            elif metric == LAST_ACCURACY:
                metric_fn.update_state(targets, predictions[LAST_PREDICTIONS])
            else:
                metric_fn.update_state(targets, predictions[PREDICTIONS])

    # def logits(
    #         self,
    #         inputs,  # {'hidden': hidden, 'encoder_output_state': encoder_output_state}
    #         target=None  # target sequence [batch_size, seq_size]
    # ):
    #     # 'hidden' shape [batch_size, seq_size, hidden_size]
    #     # 'encoder_output_state' dependent on cell_type:
    #     #      lstm: list (shape [batch_size, state_size], shape [batch_size, state_size])
    #     #      rnn, gru: list [shape [batch_size, state_size]]
    #     # return logits shape [batch_size, seq_size, num_classes]
    #
    #     return self.decoder_obj(inputs, target=target)

    def logits(
            self,
            inputs,
            target=None,
            training=None
    ):
        if isinstance(self.decoder_obj, SequenceGeneratorDecoder):
            # Generator Decoder
            if training:
                return self._logits_training(inputs, target, training)
            else:
                return self._logits_prediction(inputs)
        else:
            # Tagger Decoder
            if training:
                return self.decoder_obj(inputs)
            else:
                return self._logits_prediction(inputs)


    def _logits_training(self, inputs, target, training=None):
        input = inputs['hidden'] # shape [batch_size, seq_size, state_size]
        encoder_end_state = self._prepare_decoder_input_state(inputs)

        logits = self.decoder_obj.decoder_training(
            input,
            target=target,
            encoder_end_state=encoder_end_state
        )
        return logits  # shape = [b, s, c]

    def _logits_prediction(self, inputs):
        return inputs

    def predictions(self, inputs, training=None):

        # Generator Decoder
        if training:
            return self._predictions_training(inputs, training=training)
        else:
            return self._predictions_prediction(inputs, training=training)

    # todo tf2 need to determine if the section of code is needed
    def _predictions_training(self, inputs, training=None):    # not executed
        # inputs == logits
        probs = softmax(inputs)
        preds = tf.argmax(inputs)
        return {'predictions': preds, 'probabilities': probs}

    def _predictions_prediction(
            self,
            inputs,   # encoder_output, encoder_output_state
            training=None
    ):
        if isinstance(self.decoder_obj, SequenceGeneratorDecoder):
            encoder_output = inputs[LOGITS]['hidden'] # shape [batch_size, seq_size, state_size]
            # form dependent on cell_type
            # lstm: list([batch_size, state_size], [batch_size, state_size])
            # rnn, gru: [batch_size, state_size]
            encoder_output_state = self._prepare_decoder_input_state(inputs[LOGITS])

            logits = self.decoder_obj.decoder_inference(
                encoder_output,
                encoder_end_state=encoder_output_state,
                training=training
            )
        else:
            # Tagger Decoder  todo tf2 reconcile tensor shape for inputs[LOGITS][HIDDEN]
            logits = self.decoder_obj(inputs[LOGITS], training=training)

        probabilities = tf.nn.softmax(
            logits,
            name='probabilities_{}'.format(self.name)
        )

        if self.decoder_obj.beam_width > 1:
            predictions = self.decoder_obj.decoder_beam_search(
                encoder_output,
                encoder_end_state=encoder_output_state,
                training=training
            )
        else:
            predictions = tf.argmax(
                logits,
                -1,
                name='predictions_{}'.format(self.name),
                output_type=tf.int64
            )

        if self.decoder == 'generator':
            additional = 1  # because of eos symbol
        elif self.decoder == 'tagger':
            additional = 0
        else:
            additional = 0

        generated_sequence_lengths = sequence_length_2D(predictions)
        last_predictions = tf.gather_nd(
            predictions,
            tf.stack(
                [tf.range(tf.shape(predictions)[0]),
                 tf.maximum(
                     generated_sequence_lengths - 1 - additional,
                     0
                 )],
                axis=1
            ),
            name='last_predictions_{}'.format(self.name)
        )

        # mask logits
        mask = tf.sequence_mask(
            generated_sequence_lengths,
            maxlen=logits.shape[1],
            dtype=tf.float32
        )

        logits = logits * mask[:, :, tf.newaxis]

        return {
            PREDICTIONS: predictions,
            LAST_PREDICTIONS: last_predictions,
            PROBABILITIES: probabilities,
            LOGITS: logits
        }

    def _prepare_decoder_input_state(self, inputs):

        if 'encoder_output_state' in inputs:
            decoder_input_state = inputs['encoder_output_state']
        else:
            eo = inputs['hidden']
            if len(eo.shape) == 3:  # encoder_output is a sequence
                # reduce_sequence returns a [b, h]
                decoder_input_state = \
                    reduce_sequence(
                        eo,
                        self.reduce_input if self.reduce_input else 'sum'
                    )
            elif len(eo.shape) == 2:
                # this returns a [b, h]
                decoder_input_state = eo
            else:
                raise ValueError("Only works for 1d or 2d encoder_output")

        # now we have to deal with the fact that the state needs to be a list
        # in case of lstm or a tensor otherwise
        if (self.decoder_obj.cell_type == 'lstm' and
                isinstance(decoder_input_state, list)):
            if len(decoder_input_state) == 2:
                # this maybe a unidirection lstm or a bidirectional gru / rnn
                # there is no way to tell
                # If it is a unidirectional lstm, pass will work fine
                # if it is bidirectional gru / rnn, the output of one of
                # the directions will be treated as the inital c of the lstm
                # which is weird and may lead to poor performance
                # todo try to find a way to distinguish among these two cases
                pass
            elif len(decoder_input_state) == 4:
                # the encoder was a bidirectional lstm
                # a good strategy is to average the 2 h and the 2 c vectors
                decoder_input_state = [
                    average(
                        [decoder_input_state[0], decoder_input_state[2]]
                    ),
                    average(
                        [decoder_input_state[1], decoder_input_state[3]]
                    )
                ]
            else:
                # no idea how lists of length different than 2 or 4
                # might have been originated, we can either rise an ValueError
                # or deal with it averaging everything
                # raise ValueError(
                #     "encoder_output_state has length different than 2 or 4. "
                #     "Please doublecheck your encoder"
                # )
                average_state = average(decoder_input_state)
                decoder_input_state = [average_state, average_state]

        elif (self.decoder_obj.cell_type == 'lstm' and
              not isinstance(decoder_input_state, list)):
            decoder_input_state = [decoder_input_state, decoder_input_state]

        elif (self.decoder_obj.cell_type != 'lstm' and
              isinstance(decoder_input_state, list)):
            # here we have a couple options,
            # either reuse part of the input encoder state,
            # or just use its output
            if len(decoder_input_state) == 2:
                # using h and ignoring c
                decoder_input_state = decoder_input_state[0]
            elif len(decoder_input_state) == 4:
                # using h and ignoring c
                decoder_input_state + average(
                    [decoder_input_state[0], decoder_input_state[2]]
                )
            else:
                # no idea how lists of length different than 2 or 4
                # might have been originated, we can either rise an ValueError
                # or deal with it averaging everything
                # raise ValueError(
                #     "encoder_output_state has length different than 2 or 4. "
                #     "Please doublecheck your encoder"
                # )
                decoder_input_state = average(decoder_input_state)
            # this returns a [b, h]
            # decoder_input_state = reduce_sequence(eo, self.reduce_input)

        elif (self.decoder_obj.cell_type != 'lstm' and
              not isinstance(decoder_input_state, list)):
            # do nothing, we are good
            pass

        # at this point decoder_input_state is either a [b,h]
        # or a list([b,h], [b,h]) if the decoder cell is an lstm
        # but h may not be the same as the decoder state size,
        # so we may need to project
        if isinstance(decoder_input_state, list):
            for i in range(len(decoder_input_state)):
                if (decoder_input_state[i].shape[1] !=
                        self.decoder_obj.state_size):
                    decoder_input_state[i] = self.decoder_obj.project(
                        decoder_input_state[i]
                    )
        else:
            if decoder_input_state.shape[1] != self.decoder_obj.state_size:
                decoder_input_state = self.decoder_obj.project(
                    decoder_input_state
                )

        # make sure we are passing back the state tensors in a list
        if not isinstance(decoder_input_state, list):
            decoder_input_state = [decoder_input_state]

        return decoder_input_state

    default_validation_metric = LOSS

    @staticmethod
    def update_model_definition_with_metadata(
            output_feature,
            feature_metadata,
            *args,
            **kwargs
    ):
        output_feature['num_classes'] = feature_metadata['vocab_size']
        output_feature['max_sequence_length'] = (
            feature_metadata['max_sequence_length']
        )
        if isinstance(output_feature[LOSS]['class_weights'], (list, tuple)):
            if (len(output_feature[LOSS]['class_weights']) !=
                    output_feature['num_classes']):
                raise ValueError(
                    'The length of class_weights ({}) is not compatible with '
                    'the number of classes ({}) for feature {}. '
                    'Check the metadata JSON file to see the classes '
                    'and their order and consider there needs to be a weight '
                    'for the <UNK> and <PAD> class too.'.format(
                        len(output_feature[LOSS]['class_weights']),
                        output_feature['num_classes'],
                        output_feature['name']
                    )
                )

        if output_feature[LOSS]['class_similarities_temperature'] > 0:
            if 'class_similarities' in output_feature[LOSS]:
                similarities = output_feature[LOSS]['class_similarities']
                temperature = output_feature[LOSS][
                    'class_similarities_temperature']

                curr_row = 0
                first_row_length = 0
                is_first_row = True
                for row in similarities:
                    if is_first_row:
                        first_row_length = len(row)
                        is_first_row = False
                        curr_row += 1
                    else:
                        curr_row_length = len(row)
                        if curr_row_length != first_row_length:
                            raise ValueError(
                                'The length of row {} of the class_similarities '
                                'of {} is {}, different from the length of '
                                'the first row {}. All rows must have '
                                'the same length.'.format(
                                    curr_row,
                                    output_feature['name'],
                                    curr_row_length,
                                    first_row_length
                                )
                            )
                        else:
                            curr_row += 1
                all_rows_length = first_row_length

                if all_rows_length != len(similarities):
                    raise ValueError(
                        'The class_similarities matrix of {} has '
                        '{} rows and {} columns, '
                        'their number must be identical.'.format(
                            output_feature['name'],
                            len(similarities),
                            all_rows_length
                        )
                    )

                if all_rows_length != output_feature['num_classes']:
                    raise ValueError(
                        'The size of the class_similarities matrix of {} is '
                        '{}, different from the number of classe ({}). '
                        'Check the metadata JSON file to see the classes '
                        'and their order and '
                        'consider <UNK> and <PAD> class too.'.format(
                            output_feature['name'],
                            all_rows_length,
                            output_feature['num_classes']
                        )
                    )

                similarities = np.array(similarities, dtype=np.float32)
                for i in range(len(similarities)):
                    similarities[i, :] = softmax(
                        similarities[i, :],
                        temperature=temperature
                    )
                output_feature[LOSS]['class_similarities'] = similarities
            else:
                raise ValueError(
                    'class_similarities_temperature > 0, '
                    'but no class_similarities are provided '
                    'for feature {}'.format(output_feature['name'])
                )

        if output_feature[LOSS][TYPE] == 'sampled_softmax_cross_entropy':
            output_feature[LOSS]['class_counts'] = [
                feature_metadata['str2freq'][cls]
                for cls in feature_metadata['idx2str']
            ]


    @staticmethod
    def calculate_overall_stats(
            test_stats,
            output_feature,
            dataset,
            train_set_metadata
    ):
        feature_name = output_feature['name']
        sequences = dataset.get(feature_name)
        last_elem_sequence = sequences[np.arange(sequences.shape[0]),
                                       (sequences != 0).cumsum(1).argmax(1)]
        stats = test_stats[feature_name]
        confusion_matrix = ConfusionMatrix(
            last_elem_sequence,
            stats[LAST_PREDICTIONS],
            labels=train_set_metadata[feature_name]['idx2str']
        )
        stats['confusion_matrix'] = confusion_matrix.cm.tolist()
        stats['overall_stats'] = confusion_matrix.stats()
        stats['per_class_stats'] = confusion_matrix.per_class_stats()

    @staticmethod
    def postprocess_results(
            output_feature,
            result,
            metadata,
            experiment_dir_name,
            skip_save_unprocessed_output=False,
    ):
        postprocessed = {}
        npy_filename = os.path.join(experiment_dir_name, '{}_{}.npy')
        name = output_feature['name']

        if PREDICTIONS in result and len(result[PREDICTIONS]) > 0:
            preds = result[PREDICTIONS]
            if 'idx2str' in metadata:
                postprocessed[PREDICTIONS] = [
                    [metadata['idx2str'][token] for token in pred]
                    for pred in preds
                ]
            else:
                postprocessed[PREDICTIONS] = preds

            if not skip_save_unprocessed_output:
                np.save(npy_filename.format(name, PREDICTIONS), preds)

            del result[PREDICTIONS]

        if LAST_PREDICTIONS in result and len(result[LAST_PREDICTIONS]) > 0:
            last_preds = result[LAST_PREDICTIONS]
            if 'idx2str' in metadata:
                postprocessed[LAST_PREDICTIONS] = [
                    metadata['idx2str'][last_pred] for last_pred in last_preds
                ]
            else:
                postprocessed[LAST_PREDICTIONS] = last_preds

            if not skip_save_unprocessed_output:
                np.save(npy_filename.format(name, LAST_PREDICTIONS), last_preds)

            del result[LAST_PREDICTIONS]

        if PROBABILITIES in result and len(result[PROBABILITIES]) > 0:
            probs = result[PROBABILITIES].numpy()
            if probs is not None:

                if len(probs) > 0 and isinstance(probs[0], list):
                    prob = []
                    for i in range(len(probs)):
                        # todo: should adapt for the case of beam > 1
                        for j in range(len(probs[i])):
                            probs[i][j] = np.max(probs[i][j])
                        prob.append(np.prod(probs[i]))
                elif isinstance(probs, np.ndarray):
                    if (probs.shape) == 3:  # prob of each class of each token
                        probs = np.amax(probs, axis=-1)
                    prob = np.prod(probs, axis=-1)

                # commenting probabilities out because usually it is huge:
                # dataset x length x classes
                # todo: add a mechanism for letting the user decide to save it
                # postprocessed[PROBABILITIES] = probs
                postprocessed[PROBABILITY] = prob

                if not skip_save_unprocessed_output:
                    # commenting probabilities out, see comment above
                    # np.save(npy_filename.format(name, PROBABILITIES), probs)
                    np.save(npy_filename.format(name, PROBABILITY), prob)

            del result[PROBABILITIES]

        if LENGTHS in result:
            del result[LENGTHS]

        return postprocessed

    @staticmethod
    def populate_defaults(output_feature):
        set_default_value(
            output_feature,
            LOSS,
            {
                'type': 'softmax_cross_entropy',
                'sampler': None,
                'negative_samples': 0,
                'distortion': 1,
                'labels_smoothing': 0,
                'class_weights': 1,
                'robust_lambda': 0,
                'confidence_penalty': 0,
                'class_similarities_temperature': 0,
                'weight': 1
            }
        )
        set_default_value(output_feature[LOSS], 'type', 'softmax_cross_entropy')
        set_default_value(output_feature[LOSS], 'labels_smoothing', 0)
        set_default_value(output_feature[LOSS], 'class_weights', 1)
        set_default_value(output_feature[LOSS], 'robust_lambda', 0)
        set_default_value(output_feature[LOSS], 'confidence_penalty', 0)
        set_default_value(output_feature[LOSS],
                          'class_similarities_temperature', 0)
        set_default_value(output_feature[LOSS], 'weight', 1)

        if output_feature[LOSS][TYPE] == 'sampled_softmax_cross_entropy':
            set_default_value(output_feature[LOSS], 'sampler', 'log_uniform')
            set_default_value(output_feature[LOSS], 'negative_samples', 25)
            set_default_value(output_feature[LOSS], 'distortion', 0.75)
        else:
            set_default_value(output_feature[LOSS], 'sampler', None)
            set_default_value(output_feature[LOSS], 'negative_samples', 0)
            set_default_value(output_feature[LOSS], 'distortion', 1)

        set_default_value(output_feature[LOSS], 'unique', False)

        set_default_value(output_feature, 'decoder', 'generator')

        if output_feature['decoder'] == 'tagger':
            set_default_value(output_feature, 'reduce_input', None)

        set_default_value(output_feature, 'dependencies', [])
        set_default_value(output_feature, 'reduce_input', SUM)
        set_default_value(output_feature, 'reduce_dependencies', SUM)

    decoder_registry = {
        'generator': SequenceGeneratorDecoder,
        'tagger': SequenceTaggerDecoder
    }




