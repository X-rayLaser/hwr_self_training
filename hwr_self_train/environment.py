import os

from torch.utils.data import DataLoader

import configuration
from hwr_self_train.utils import collate
from .models import ImageEncoder, AttendingDecoder
from .recognition import (
    WordRecognitionPipeline,
    TrainableEncoderDecoder
)
from .image_pipelines import make_pretraining_pipeline, make_validation_pipeline
from .datasets import (
    SyntheticOnlineDataset,
    SyntheticOnlineDatasetCached,
    UnlabeledDataset,
    LabeledDataset
)

from .checkpoints import (
    CheckpointKeeper,
    CheckpointsNotFound
)
from .training import TrainingLoop, Trainer
from .history_saver import HistoryCsvSaver
from .evaluation import EvaluationTask

from configuration import (
    tokenizer,
    Configuration,
    prepare_metrics,
    prepare_loss,
    create_optimizer
)

from hwr_self_train.training import ConsistencyTrainer
from hwr_self_train.augmentation import WeakAugmentation, StrongAugmentation


def create_neural_pipeline(device):
    encoder = ImageEncoder(image_height=Configuration.image_height,
                           hidden_size=Configuration.hidden_size)

    context_size = encoder.hidden_size * 2
    decoder_hidden_size = encoder.hidden_size

    sos_token = tokenizer.char2index[tokenizer.start]
    decoder = AttendingDecoder(sos_token, context_size, y_size=tokenizer.charset_size,
                               hidden_size=decoder_hidden_size)

    encoder_optimizer = create_optimizer(encoder, Configuration.encoder_optimizer)
    decoder_optimizer = create_optimizer(decoder, Configuration.decoder_optimizer)
    return TrainableEncoderDecoder(
        encoder, decoder, device, encoder_optimizer, decoder_optimizer
    )


def load_or_create_neural_pipeline():
    neural_pipeline = create_neural_pipeline(Configuration.device)
    keeper = CheckpointKeeper(Configuration.checkpoints_save_dir)

    try:
        keeper.load_latest_checkpoint(neural_pipeline, Configuration.device)
        return neural_pipeline
    except CheckpointsNotFound:
        # since checkpoints do not exist, assume that we start from scratch,
        # therefore we remove existing history file
        if os.path.isfile(Configuration.history_path):
            os.remove(Configuration.history_path)

        neural_pipeline.encoder.to(neural_pipeline.device)
        neural_pipeline.decoder.to(neural_pipeline.device)
        keeper.make_new_checkpoint(neural_pipeline, Configuration.device, 0, metrics={})
        return neural_pipeline


class Environment:
    def __init__(self):
        self._make_checkpoints_dir()

        self.neural_pipeline = load_or_create_neural_pipeline()

        image_pipeline = make_pretraining_pipeline(
            augmentation_options=Configuration.weak_augment_options,
            max_heights=Configuration.image_height
        )
        recognizer = WordRecognitionPipeline(self.neural_pipeline, tokenizer, image_pipeline)

        loss_fn = prepare_loss(Configuration.loss_function)

        train_metric_fns = prepare_metrics(Configuration.training_metrics)
        train_val_metric_fns = prepare_metrics(Configuration.train_val_metrics)
        val_metric_fns = prepare_metrics(Configuration.validation_metrics)
        test_metric_fns = prepare_metrics(Configuration.test_metrics)

        training_loader = self._create_data_loader(SyntheticOnlineDataset,
                                                   Configuration.training_set_size)

        val_loader = self._create_data_loader(SyntheticOnlineDatasetCached,
                                              Configuration.validation_set_size)

        test_ds = LabeledDataset(Configuration.iam_dataset_path)
        test_loader = DataLoader(test_ds, batch_size=Configuration.batch_size,
                                 num_workers=Configuration.num_workers, collate_fn=collate)

        trainer = Trainer(recognizer, training_loader, loss_fn, tokenizer)

        self.epochs_trained = self._get_trained_epochs()

        self.training_loop = TrainingLoop(trainer, metric_fns=train_metric_fns,
                                          epochs=Configuration.epochs,
                                          starting_epoch=self.epochs_trained + 1)

        self.history_saver = HistoryCsvSaver(Configuration.history_path)

        eval_on_train = EvaluationTask(recognizer, training_loader, train_metric_fns,
                                       Configuration.evaluation_steps['training_set'])

        eval_on_train_val = EvaluationTask(recognizer, val_loader, train_val_metric_fns,
                                           Configuration.evaluation_steps['train_validation_set'])

        val_preprocessor = make_validation_pipeline(max_heights=Configuration.image_height)
        val_recognizer = WordRecognitionPipeline(self.neural_pipeline, tokenizer, val_preprocessor)
        eval_on_val = EvaluationTask(val_recognizer, val_loader, val_metric_fns,
                                     Configuration.evaluation_steps['validation_set'])

        eval_on_test = EvaluationTask(val_recognizer, test_loader, test_metric_fns,
                                      num_batches=Configuration.evaluation_steps['test_set'])
        self.eval_tasks = [eval_on_train, eval_on_train_val, eval_on_val, eval_on_test]

    def save_checkpoint(self, epoch, metrics):
        keeper = CheckpointKeeper(Configuration.checkpoints_save_dir)
        keeper.make_new_checkpoint(self.neural_pipeline, Configuration.device, epoch, metrics)

    def _get_trained_epochs(self):
        try:
            keeper = CheckpointKeeper(Configuration.checkpoints_save_dir)
            meta_data = keeper.get_latest_meta_data()
            return meta_data["epoch"]
        except CheckpointsNotFound:
            return 0

    def _make_checkpoints_dir(self):
        os.makedirs(Configuration.checkpoints_save_dir, exist_ok=True)

    def _create_data_loader(self, dataset_class, dataset_size):
        ds = dataset_class(
            Configuration.fonts_dir, dataset_size,
            dict_file=Configuration.dictionary_file
        )

        return DataLoader(ds, batch_size=Configuration.batch_size,
                          num_workers=Configuration.num_workers, collate_fn=collate)


class TuningEnvironment:
    def __init__(self):
        pseudo_labeled_dataset = LabeledDataset(Configuration.iam_pseudo_labels)
        pseudo_labeled_loader = self._create_loader(pseudo_labeled_dataset)

        training_dataset = LabeledDataset(Configuration.iam_train_path)
        training_loader = self._create_loader(training_dataset)

        test_ds = LabeledDataset(Configuration.iam_dataset_path)
        test_loader = self._create_loader(test_ds)

        unlabeled_ds = UnlabeledDataset(Configuration.iam_train_path)
        unlabeled_loader = self._create_loader(unlabeled_ds)

        encoder_decoder = load_or_create_neural_pipeline()
        image_preprocessor = make_validation_pipeline(max_heights=Configuration.image_height)
        recognizer = WordRecognitionPipeline(encoder_decoder, tokenizer, image_preprocessor)

        metric_fns = prepare_metrics(Configuration.training_metrics)

        test_metrics = prepare_metrics(Configuration.test_metrics)

        eval_steps = Configuration.evaluation_steps

        self.pseudo_labeled_dataset = pseudo_labeled_dataset
        self.pseudo_labeled_loader = pseudo_labeled_loader
        self.unlabeled_loader = unlabeled_loader
        self.tokenizer = tokenizer
        self.pseudo_labels_path = Configuration.iam_pseudo_labels
        self.threshold = Configuration.confidence_threshold
        self.tuning_epochs = Configuration.tuning_epochs
        self.recognizer = recognizer
        self.metric_fns = metric_fns

        self.tasks = [EvaluationTask(recognizer, training_loader, self.metric_fns,
                                     num_batches=eval_steps["training_set"]),
                      EvaluationTask(recognizer, test_loader, test_metrics,
                                     num_batches=eval_steps["test_set"])]

        self.history_saver = HistoryCsvSaver('tuning_history.csv')

    def _create_loader(self, ds):
        return DataLoader(ds,
                          batch_size=Configuration.batch_size,
                          num_workers=Configuration.num_workers,
                          collate_fn=collate)

    def get_trainer(self):
        self.pseudo_labeled_dataset.re_build()
        loader = self._create_loader(self.pseudo_labeled_dataset)

        loss_fn = prepare_loss(Configuration.loss_function)
        weak_augment = WeakAugmentation(**Configuration.weak_augment_options)
        strong_augment = StrongAugmentation()
        return ConsistencyTrainer(self.recognizer, loader, loss_fn,
                                  tokenizer, weak_augment, strong_augment)
