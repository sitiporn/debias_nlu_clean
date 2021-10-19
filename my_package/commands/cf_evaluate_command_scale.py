"""
The `evaluate` subcommand can be used to
evaluate a trained model against a dataset
and report any metrics calculated by the model.
"""

import argparse
import json
import logging
import pickle

from copy import deepcopy

from overrides import overrides

from allennlp.commands.subcommand import Subcommand
from allennlp.common import logging as common_logging
from allennlp.common.util import prepare_environment
from allennlp.models.archival import load_archive
from allennlp.training.util import evaluate

import datetime
import os
import shutil
from os import PathLike
from typing import Any, Dict, Iterable, Optional, Union, Tuple, Set, List
from collections import Counter

import torch
from torch.nn.utils import clip_grad_norm_

from allennlp.common.checks import check_for_gpu, ConfigurationError
from allennlp.common.params import Params
from allennlp.common.tqdm import Tqdm
from allennlp.common.util import dump_metrics, sanitize, int_to_device
from allennlp.data import Instance, Vocabulary, Batch, DataLoader
from allennlp.data.dataset_readers import DatasetReader
from allennlp.models.archival import CONFIG_NAME
from allennlp.models.model import Model
from allennlp.nn import util as nn_util


from my_package.data.dataset_readers.counterfactual_reader import (
    CounterfactualSnliReader,
)

from allennlp.data.token_indexers import (
    SingleIdTokenIndexer,
    TokenCharactersIndexer,
    ELMoTokenCharactersIndexer,
    PretrainedTransformerIndexer,
    PretrainedTransformerMismatchedIndexer,
)
from allennlp.data.tokenizers import (
    CharacterTokenizer,
    PretrainedTransformerTokenizer,
    SpacyTokenizer,
    WhitespaceTokenizer,
)

from allennlp.training.metrics import CategoricalAccuracy


# We want to warn people that tqdm ignores metrics that start with underscores
# exactly once. This variable keeps track of whether we have.
class HasBeenWarned:
    tqdm_ignores_underscores = False


logger = logging.getLogger(__name__)


def temperature_scale(temperature, logits):
    """
    Perform temperature scaling on logits
    """
    # Expand temperature to match the size of logits
    temperature = temperature.unsqueeze(1).expand(
        logits.size(0), logits.size(1)
    )
    return logits / temperature

def cf_evaluate(
    model: Model,
    data_loader: DataLoader,
    cf_weight: float,
    pickled_temperature_path: str,
    cf_method: str = "mult",
    cuda_device: int = -1,
    batch_weight_key: str = None,
    output_file: str = None,
    predictions_output_file: str = None,
) -> Dict[str, Any]:
    """
    # Parameters
    model : `Model`
        The model to evaluate
    data_loader : `DataLoader`
        The `DataLoader` that will iterate over the evaluation data (data loaders already contain
        their data).
    cuda_device : `int`, optional (default=`-1`)
        The cuda device to use for this evaluation.  The model is assumed to already be using this
        device; this parameter is only used for moving the input data to the correct device.
    batch_weight_key : `str`, optional (default=`None`)
        If given, this is a key in the output dictionary for each batch that specifies how to weight
        the loss for that batch.  If this is not given, we use a weight of 1 for every batch.
    metrics_output_file : `str`, optional (default=`None`)
        Optional path to write the final metrics to.
    predictions_output_file : `str`, optional (default=`None`)
        Optional path to write the predictions to.
    # Returns
    `Dict[str, Any]`
        The final metrics.
    """
    check_for_gpu(cuda_device)
    data_loader.set_target_device(int_to_device(cuda_device))
    predictions_file = (
        None if predictions_output_file is None else open(predictions_output_file, "w")
    )
    temperature = pickle.load( open( pickled_temperature_path, "rb" ) )
    with torch.no_grad():
        model.eval()
        iterator = iter(data_loader)
        logger.info("Iterating over dataset")
        generator_tqdm = Tqdm.tqdm(iterator)

        # Number of batches in instances.
        batch_count = 0
        # Number of batches where the model produces a loss.
        loss_count = 0
        # Cumulative weighted loss
        total_loss = 0.0
        # Cumulative weight across all batches.
        total_weight = 0.0
        # Init loss and accuracy
        cf_accuracy = CategoricalAccuracy()
        cf_loss = torch.nn.CrossEntropyLoss()
        metrics = {"accuracy": cf_accuracy.get_metric(False)}
        for batch in generator_tqdm:
            # create cf batch
            batch_cf = deepcopy(batch)
            batch_cf["tokens"] = batch_cf.pop("cf_tokens")
            batch.pop("cf_tokens")

            batch_count += 1
            batch = nn_util.move_to_device(batch, cuda_device)
            output_dict = model(**batch)
            output_dict_cf = model(**batch_cf)
            #scale 
            output_dict["logits"] = temperature_scale(temperature,output_dict["logits"])
            output_dict_cf["logits"] = temperature_scale(temperature,output_dict_cf["logits"])

            if cf_method == "mult":
                output_dict["logits"] = (
                    output_dict["logits"] - cf_weight * output_dict_cf["logits"]
                )
                probs = torch.nn.functional.softmax(output_dict["logits"], dim=-1)
                output_dict["probs"] = probs
            elif cf_method == "entropy":
                n_class = torch.tensor(output_dict["logits"].shape[1])
                factual_softmax = torch.nn.functional.softmax(
                    output_dict["logits"], dim=-1
                )
                cf_softmax = torch.nn.functional.softmax(
                    output_dict_cf["logits"], dim=-1
                )
                factual_entropy = -torch.sum(
                    factual_softmax * torch.log(factual_softmax) / torch.log(n_class),
                    dim=1,
                )
                output_dict["probs"] = factual_softmax - (
                    factual_entropy.unsqueeze(1) * cf_weight * cf_softmax
                )
            elif cf_method == "add":
                sample_weight = batch["sample_weight"].view(
                    batch["sample_weight"].shape[0], 1
                )
                output_dict["logits"] = (
                    output_dict["logits"]
                    - (cf_weight + sample_weight) * output_dict_cf["logits"]
                )
                probs = torch.nn.functional.softmax(output_dict["logits"], dim=-1)
                output_dict["probs"] = probs
            # loss = output_dict.get("loss")
            # model._accuracy(output_dict['logits'], batch['label'])
            loss = cf_loss(output_dict["logits"], batch["label"].long().view(-1))  # ??
            output_dict["loss"] = loss
            cf_accuracy(output_dict["probs"], batch["label"])
            # metrics = model.get_metrics()
            metrics["accuracy"] = cf_accuracy.get_metric()
            if loss is not None:
                loss_count += 1
                if batch_weight_key:
                    weight = output_dict[batch_weight_key].item()
                else:
                    weight = 1.0

                total_weight += weight
                total_loss += loss.item() * weight
                # Report the average loss so far.
                metrics["loss"] = total_loss / total_weight

            if not HasBeenWarned.tqdm_ignores_underscores and any(
                metric_name.startswith("_") for metric_name in metrics
            ):
                logger.warning(
                    'Metrics with names beginning with "_" will '
                    "not be logged to the tqdm progress bar."
                )
                HasBeenWarned.tqdm_ignores_underscores = True
            description = (
                ", ".join(
                    [
                        "%s: %.2f" % (name, value)
                        for name, value in metrics.items()
                        if not name.startswith("_")
                    ]
                )
                + " ||"
            )
            generator_tqdm.set_description(description, refresh=False)

            if predictions_file is not None:
                predictions = json.dumps(
                    sanitize(model.make_output_human_readable(output_dict))
                )
                predictions_file.write(predictions + "\n")

        if predictions_file is not None:
            predictions_file.close()

        # recaculate accuracy
        # model._accuracy(output_dict['logits'], batch['label'])
        # final_metrics = model.get_metrics(reset=True)
        final_metrics = metrics
        if loss_count > 0:
            # Sanity check
            if loss_count != batch_count:
                raise RuntimeError(
                    "The model you are trying to evaluate only sometimes produced a loss!"
                )
            final_metrics["loss"] = total_loss / total_weight
            # final_metrics["loss"] = "N/A"
        if output_file is not None:
            dump_metrics(output_file, final_metrics, log=True)

        return final_metrics


@Subcommand.register("evaluate_mult_cf_scale")
class Evaluate(Subcommand):
    @overrides
    def add_subparser(
        self, parser: argparse._SubParsersAction
    ) -> argparse.ArgumentParser:
        description = """Evaluate the specified model + dataset"""
        subparser = parser.add_parser(
            self.name,
            description=description,
            help="Evaluate the specified model + dataset.",
        )

        subparser.add_argument(
            "archive_file", type=str, help="path to an archived trained model"
        )

        subparser.add_argument(
            "input_file",
            type=str,
            help="path to the file containing the evaluation data",
        )

        subparser.add_argument(
            "temperature_file",
            type=str,
            help="path to temperature",
        )

        subparser.add_argument(
            "--output-file",
            type=str,
            help="optional path to write the metrics to as JSON",
        )

        subparser.add_argument(
            "--predictions-output-file",
            type=str,
            help="optional path to write the predictions to as JSON lines",
        )

        subparser.add_argument(
            "--weights-file",
            type=str,
            help="a path that overrides which weights file to use",
        )

        cuda_device = subparser.add_mutually_exclusive_group(required=False)
        cuda_device.add_argument(
            "--cuda-device", type=int, default=-1, help="id of GPU to use (if any)"
        )

        subparser.add_argument(
            "-o",
            "--overrides",
            type=str,
            default="",
            help=(
                "a json(net) structure used to override the experiment configuration, e.g., "
                "'{\"iterator.batch_size\": 16}'.  Nested parameters can be specified either"
                " with nested dictionaries or with dot syntax."
            ),
        )

        subparser.add_argument(
            "--batch-size",
            type=int,
            help="If non-empty, the batch size to use during evaluation.",
        )

        subparser.add_argument(
            "--batch-weight-key",
            type=str,
            default="",
            help="If non-empty, name of metric used to weight the loss on a per-batch basis.",
        )

        subparser.add_argument(
            "--extend-vocab",
            action="store_true",
            default=False,
            help="if specified, we will use the instances in your new dataset to "
            "extend your vocabulary. If pretrained-file was used to initialize "
            "embedding layers, you may also need to pass --embedding-sources-mapping.",
        )

        subparser.add_argument(
            "--embedding-sources-mapping",
            type=str,
            default="",
            help="a JSON dict defining mapping from embedding module path to embedding "
            "pretrained-file used during training. If not passed, and embedding needs to be "
            "extended, we will try to use the original file paths used during training. If "
            "they are not available we will use random vectors for embedding extension.",
        )
        subparser.add_argument(
            "--file-friendly-logging",
            action="store_true",
            default=False,
            help="outputs tqdm status on separate lines and slows tqdm refresh rate",
        )
        subparser.add_argument(
            "--cf_type",
            type=str,
            default="counterfactual_snli",
            help="counterfactual type",
        )

        subparser.add_argument(
            "--cf_weight",
            type=float,
            default=0.5,
            help="weight for counterfactual component",
        )

        subparser.add_argument(
            "--cf_method", type=str, default="mult", help="counterfactual type"
        )

        subparser.set_defaults(func=evaluate_from_args)

        return subparser


def evaluate_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    common_logging.FILE_FRIENDLY_LOGGING = args.file_friendly_logging

    # Disable some of the more verbose logging statements
    logging.getLogger("allennlp.common.params").disabled = True
    logging.getLogger("allennlp.nn.initializers").disabled = True
    logging.getLogger("allennlp.modules.token_embedders.embedding").setLevel(
        logging.INFO
    )

    # Load from archive
    archive = load_archive(
        args.archive_file,
        weights_file=args.weights_file,
        cuda_device=args.cuda_device,
        overrides=args.overrides,
    )
    config = deepcopy(archive.config)
    prepare_environment(config)
    model = archive.model
    model.eval()

    # Load the evaluation data
    model_config = config.get("model")
    model_name = model_config["text_field_embedder"]["token_embedders"]["tokens"][
        "model_name"
    ]
    max_length = model_config["text_field_embedder"]["token_embedders"]["tokens"][
        "max_length"
    ]

    pretrained_transformer_tokenizer = PretrainedTransformerTokenizer(
        model_name=model_name, add_special_tokens=False
    )
    token_indexer = PretrainedTransformerIndexer(
        model_name=model_name, max_length=max_length
    )
    dataset_reader = DatasetReader.from_params(
        args.cf_type,
        tokenizer=pretrained_transformer_tokenizer,
        token_indexers={"tokens": token_indexer},
    )
    # dataset_reader = CounterfactualSnliReader(tokenizer=pretrained_transformer_tokenizer,token_indexers={"tokens":token_indexer})
    # dataset_reader = archive.validation_dataset_reader

    # split files
    evaluation_data_path_list = args.input_file.split(":")
    if args.output_file != None:
        output_file_list = args.output_file.split(":")
        assert len(output_file_list) == len(
            evaluation_data_path_list
        ), "number of output path must be equal number of dataset "
    if args.predictions_output_file != None:
        predictions_output_file_list = args.predictions_output_file.split(";")
        assert len(predictions_output_file_list) == len(
            evaluation_data_path_list
        ), "number of predictions_output_file path must be equal number of dataset "

    # output file
    output_file_path = None
    predictions_output_file_path = None

    for index in range(len(evaluation_data_path_list)):
        config = deepcopy(archive.config)
        evaluation_data_path = evaluation_data_path_list[index]
        if args.output_file != None:
            output_file_path = output_file_list[index]
        if args.predictions_output_file != None:
            predictions_output_file_path = predictions_output_file_list[index]

        logger.info("Reading evaluation data from %s", evaluation_data_path)
        data_loader_params = config.get("validation_data_loader", None)
        if data_loader_params is None:
            data_loader_params = config.get("data_loader")
        if args.batch_size:
            data_loader_params["batch_size"] = args.batch_size
        data_loader = DataLoader.from_params(
            params=data_loader_params,
            reader=dataset_reader,
            data_path=evaluation_data_path,
        )

        embedding_sources = (
            json.loads(args.embedding_sources_mapping)
            if args.embedding_sources_mapping
            else {}
        )

        if args.extend_vocab:
            logger.info("Vocabulary is being extended with test instances.")
            model.vocab.extend_from_instances(instances=data_loader.iter_instances())
            model.extend_embedder_vocab(embedding_sources)

        data_loader.index_with(model.vocab)

        metrics = cf_evaluate(
            model,
            data_loader,
            args.cf_weight,
            args.temperature_file,
            args.cf_method,
            args.cuda_device,
            args.batch_weight_key,
            output_file=output_file_path,
            predictions_output_file=predictions_output_file_path,
        )
    logger.info("Finished evaluating.")

    return metrics