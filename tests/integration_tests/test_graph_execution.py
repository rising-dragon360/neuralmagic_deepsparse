# Copyright (c) 2020 Uber Technologies, Inc.
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
import pytest

from tests.integration_tests.utils import (
    category_feature,
    generate_data,
    generate_output_features_with_dependencies,
    numerical_feature,
    run_experiment,
    sequence_feature,
    set_feature,
    text_feature,
)


@pytest.mark.distributed
@pytest.mark.parametrize(
    "output_features",
    [
        # baseline test case
        [
            category_feature(vocab_size=2, reduce_input="sum"),
            sequence_feature(vocab_size=10, max_len=5),
            numerical_feature(),
        ],
        # use generator as decoder
        [
            category_feature(vocab_size=2, reduce_input="sum"),
            sequence_feature(vocab_size=10, max_len=5, decoder="generator"),
            numerical_feature(),
        ],
        # Generator decoder and reduce_input = None
        [
            category_feature(vocab_size=2, reduce_input="sum"),
            sequence_feature(max_len=5, decoder="generator", reduce_input=None),
            numerical_feature(normalization="minmax"),
        ],
        # output features with dependencies single dependency
        generate_output_features_with_dependencies("numerical_feature", ["category_feature"]),
        # output features with dependencies multiple dependencies
        generate_output_features_with_dependencies("numerical_feature", ["category_feature", "sequence_feature"]),
    ],
)
def test_experiment_multiple_seq_seq(csv_filename, output_features):
    input_features = [
        text_feature(vocab_size=100, min_len=1, encoder="stacked_cnn"),
        numerical_feature(normalization="zscore"),
        category_feature(vocab_size=10, embedding_size=5),
        set_feature(),
        sequence_feature(vocab_size=10, max_len=10, encoder="embed"),
    ]
    output_features = output_features

    rel_path = generate_data(input_features, output_features, csv_filename)
    run_experiment(input_features, output_features, dataset=rel_path)
