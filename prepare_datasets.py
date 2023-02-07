import argparse
import json
import os
from hwr_self_train.formatters import show_progress_bar
from hwr_self_train.utils import instantiate_class


def create_unlabeled_dataset(data_importer, destination_dir):
    unlabeled_dir = os.path.join(destination_dir, 'unlabeled')
    os.makedirs(unlabeled_dir, exist_ok=True)

    gen = enumerate(data_importer.get_images())
    for i, image in show_progress_bar(gen, desc='Saving images: '):
        image.save(os.path.join(unlabeled_dir, f'{i}.png'))

    spaces = ' ' * 150
    print(f'\r{spaces}')


def create_labeled_dataset(data_importer, destination_dir):
    labeled_dir = os.path.join(destination_dir, 'labeled')

    os.makedirs(labeled_dir, exist_ok=True)

    if data_importer.get_transcribed_images() is not None:
        gen = enumerate(data_importer.get_transcribed_images())
        for i, (image, transcript) in show_progress_bar(gen, desc='Saving images: '):
            image.save(os.path.join(labeled_dir, f'{i}.png'))

            transcripts_path = os.path.join(labeled_dir, 'transcripts.txt')
            with open(transcripts_path, 'a') as f:
                f.write(transcript + '\n')

    spaces = ' ' * 150
    print(f'\r{spaces}')


def prepare_dataset(args):
    destination_dir = args.dest
    data_importer = instantiate_class(args.data_importer, **args.kwargs)
    create_unlabeled_dataset(data_importer, destination_dir)
    create_labeled_dataset(data_importer, destination_dir)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Prepare real handwriting data to be used for adaptation stage'
    )

    parser.add_argument('data_importer', type=str,
                        help='Fully qualified path (dotted) to Data Importer class')

    parser.add_argument('--kwargs', type=json.loads, default={},
                        help='Optional keyword arguments in JSON format passed to data importer constructor')

    parser.add_argument('--dest', type=str, default='tuning_data',
                        help='Output directory that will store prepared datasets')

    cmd_args = parser.parse_args()

    prepare_dataset(cmd_args)
