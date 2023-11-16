import random
from typing import Any, Dict, Optional

import torch
import torchaudio as ta
from lightning import LightningDataModule
from torch.utils.data.dataloader import DataLoader

from pflow.text import text_to_sequence
from pflow.utils.audio import mel_spectrogram
from pflow.utils.model import fix_len_compatibility, normalize
from pflow.utils.utils import intersperse


def parse_filelist(filelist_path, split_char="|"):
    with open(filelist_path, encoding="utf-8") as f:
        filepaths_and_text = [line.strip().split(split_char) for line in f]
    return filepaths_and_text


class TextMelDataModule(LightningDataModule):
    def __init__(  # pylint: disable=unused-argument
        self,
        name,
        train_filelist_path,
        valid_filelist_path,
        batch_size,
        num_workers,
        pin_memory,
        cleaners,
        add_blank,
        n_spks,
        n_fft,
        n_feats,
        sample_rate,
        hop_length,
        win_length,
        f_min,
        f_max,
        data_statistics,
        seed,
    ):
        super().__init__()

        # this line allows to access init params with 'self.hparams' attribute
        # also ensures init params will be stored in ckpt
        self.save_hyperparameters(logger=False)

    def setup(self, stage: Optional[str] = None):  # pylint: disable=unused-argument
        """Load data. Set variables: `self.data_train`, `self.data_val`, `self.data_test`.

        This method is called by lightning with both `trainer.fit()` and `trainer.test()`, so be
        careful not to execute things like random split twice!
        """
        # load and split datasets only if not loaded already

        self.trainset = TextMelDataset(  # pylint: disable=attribute-defined-outside-init
            self.hparams.train_filelist_path,
            self.hparams.n_spks,
            self.hparams.cleaners,
            self.hparams.add_blank,
            self.hparams.n_fft,
            self.hparams.n_feats,
            self.hparams.sample_rate,
            self.hparams.hop_length,
            self.hparams.win_length,
            self.hparams.f_min,
            self.hparams.f_max,
            self.hparams.data_statistics,
            self.hparams.seed,
        )
        self.validset = TextMelDataset(  # pylint: disable=attribute-defined-outside-init
            self.hparams.valid_filelist_path,
            self.hparams.n_spks,
            self.hparams.cleaners,
            self.hparams.add_blank,
            self.hparams.n_fft,
            self.hparams.n_feats,
            self.hparams.sample_rate,
            self.hparams.hop_length,
            self.hparams.win_length,
            self.hparams.f_min,
            self.hparams.f_max,
            self.hparams.data_statistics,
            self.hparams.seed,
        )

    def train_dataloader(self):
        return DataLoader(
            dataset=self.trainset,
            batch_size=self.hparams.batch_size,
            num_workers=self.hparams.num_workers,
            pin_memory=self.hparams.pin_memory,
            shuffle=True,
            collate_fn=TextMelBatchCollate(self.hparams.n_spks),
        )

    def val_dataloader(self):
        return DataLoader(
            dataset=self.validset,
            batch_size=self.hparams.batch_size,
            num_workers=self.hparams.num_workers,
            pin_memory=self.hparams.pin_memory,
            shuffle=False,
            collate_fn=TextMelBatchCollate(self.hparams.n_spks),
        )

    def teardown(self, stage: Optional[str] = None):
        """Clean up after fit or test."""
        pass  # pylint: disable=unnecessary-pass

    def state_dict(self):  # pylint: disable=no-self-use
        """Extra things to save to checkpoint."""
        return {}

    def load_state_dict(self, state_dict: Dict[str, Any]):
        """Things to do when loading checkpoint."""
        pass  # pylint: disable=unnecessary-pass


class TextMelDataset(torch.utils.data.Dataset):
    def __init__(
        self,
        filelist_path,
        n_spks,
        cleaners,
        add_blank=True,
        n_fft=1024,
        n_mels=80,
        sample_rate=22050,
        hop_length=256,
        win_length=1024,
        f_min=0.0,
        f_max=8000,
        data_parameters=None,
        seed=None,
    ):
        self.filepaths_and_text = parse_filelist(filelist_path)
        self.n_spks = n_spks
        self.cleaners = cleaners
        self.add_blank = add_blank
        self.n_fft = n_fft
        self.n_mels = n_mels
        self.sample_rate = sample_rate
        self.hop_length = hop_length
        self.win_length = win_length
        self.f_min = f_min
        self.f_max = f_max
        if data_parameters is not None:
            self.data_parameters = data_parameters
        else:
            self.data_parameters = {"mel_mean": 0, "mel_std": 1}
        random.seed(seed)
        random.shuffle(self.filepaths_and_text)

    def get_datapoint(self, filepath_and_text):
        if self.n_spks > 1:
            filepath, spk, text = (
                filepath_and_text[0],
                int(filepath_and_text[1]),
                filepath_and_text[2],
            )
        else:
            filepath, text = filepath_and_text[0], filepath_and_text[1]
            spk = None

        text = self.get_text(text, add_blank=self.add_blank)
        mel, audio = self.get_mel(filepath)
        # TODO: make dictionary to get different spec for same speaker
        # right now naively repeating target mel for testing purposes
        return {"x": text, "y": mel, "spk": spk, "wav":audio}

    def get_mel(self, filepath):
        audio, sr = ta.load(filepath)
        assert sr == self.sample_rate
        mel = mel_spectrogram(
            audio,
            self.n_fft,
            self.n_mels,
            self.sample_rate,
            self.hop_length,
            self.win_length,
            self.f_min,
            self.f_max,
            center=False,
        ).squeeze()
        mel = normalize(mel, self.data_parameters["mel_mean"], self.data_parameters["mel_std"])
        return mel, audio

    def get_text(self, text, add_blank=True):
        text_norm = text_to_sequence(text, self.cleaners)
        if self.add_blank:
            text_norm = intersperse(text_norm, 0)
        text_norm = torch.IntTensor(text_norm)
        return text_norm

    def __getitem__(self, index):
        datapoint = self.get_datapoint(self.filepaths_and_text[index])
        if datapoint["wav"].shape[-1] < 66150:
            # skip datapoint if too short (3s)
            return self.__getitem__(random.randint(0, len(self.filepaths_and_text)-1))
        return datapoint

    def __len__(self):
        return len(self.filepaths_and_text)

from audiolm_pytorch import EncodecWrapper
class TextMelBatchCollate:
    def __init__(self, n_spks, get_codes=True):
        self.n_spks = n_spks
        self.get_codes = get_codes
        if self.get_codes:
            self.encodec = EncodecWrapper()
        
    def batched_encodec(self, wav):
        with torch.no_grad():
            self.encodec.eval()
            prompts, _, _ = self.encodec(wav, curtail_from_left = True, return_encoded = True)
        return prompts
    
    def __call__(self, batch):
        B = len(batch)
        y_max_length = max([item["y"].shape[-1] for item in batch])
        y_max_length = fix_len_compatibility(y_max_length)
        wav_max_length = y_max_length * 256
        x_max_length = max([item["x"].shape[-1] for item in batch])
        n_feats = batch[0]["y"].shape[-2]

        y = torch.zeros((B, n_feats, y_max_length), dtype=torch.float32)
        x = torch.zeros((B, x_max_length), dtype=torch.long)
        wav = torch.zeros((B, 1, wav_max_length), dtype=torch.float32)
        y_lengths, x_lengths = [], []
        wav_lengths = []
        spks = []
        for i, item in enumerate(batch):
            y_, x_ = item["y"], item["x"]
            wav_ = item["wav"][:,:wav_max_length] if item["wav"].shape[-1] > wav_max_length else item["wav"]
            y_lengths.append(y_.shape[-1])
            x_lengths.append(x_.shape[-1])
            wav_lengths.append(wav_.shape[-1])
            y[i, :, : y_.shape[-1]] = y_
            x[i, : x_.shape[-1]] = x_
            wav[i, :, : wav_.shape[-1]] = wav_
            spks.append(item["spk"])
        
        if self.get_codes:
            codes = self.batched_encodec(wav)
            codes_lengths = [1 + wav_len // 320 for wav_len in wav_lengths]
            codes = codes.squeeze(0).transpose(1,2)
        
        y_lengths = torch.tensor(y_lengths, dtype=torch.long)
        x_lengths = torch.tensor(x_lengths, dtype=torch.long)
        wav_lengths = torch.tensor(wav_lengths, dtype=torch.long)
        spks = torch.tensor(spks, dtype=torch.long) if self.n_spks > 1 else None
        codes_lengths = torch.tensor(codes_lengths, dtype=torch.long) if self.get_codes else None
        return {
            "x": x, 
            "x_lengths": x_lengths, 
            "y": y, 
            "y_lengths": y_lengths, 
            "spks": spks, 
            "wav":wav, 
            "wav_lengths":wav_lengths,
            "codes":codes,
            "codes_lengths":codes_lengths,
            "prompt_spec": y,
            "prompt_lengths": y_lengths,
            "prompt_codes": codes,
            "prompt_codes_lengths": codes_lengths,
            }