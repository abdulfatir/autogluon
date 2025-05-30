import logging
from typing import Optional

import torch
from torch import nn

from ..constants import (
    COLUMN,
    COLUMN_FEATURES,
    FEATURES,
    IMAGE,
    IMAGE_VALID_NUM,
    LABEL,
    LOGIT_SCALE,
    LOGITS,
    MASKS,
    TEXT_TOKEN_IDS,
    TEXT_VALID_LENGTH,
)
from .utils import (
    assign_layer_ids,
    get_column_features,
    get_hf_config_and_model,
    get_image_size_mean_std,
    get_pretrained_tokenizer,
    get_text_segment_num,
    get_text_token_max_len,
    init_weights,
    replace_missing_images_with_learnable,
)

logger = logging.getLogger(__name__)


class CLIPForImageText(nn.Module):
    """
    Support the CLIP model.
    Refer to https://huggingface.co/docs/transformers/model_doc/clip
    """

    def __init__(
        self,
        prefix: str,
        checkpoint_name: str,
        num_classes: Optional[int] = None,
        pretrained: Optional[bool] = True,
        tokenizer_name: Optional[str] = "clip",
        has_image: Optional[bool] = True,
        has_text: Optional[bool] = True,
        image_size: Optional[int] = None,
        image_norm: Optional[str] = None,
        image_chan_num: Optional[int] = 3,
        use_learnable_image: Optional[bool] = False,
        max_text_len: Optional[int] = None,
        text_segment_num: Optional[int] = 1,
        is_matching: Optional[bool] = False,
    ):
        """
        Load the pretrained CLIP from huggingface transformers.

        Parameters
        ----------
        prefix
            The model prefix.
        checkpoint_name
            Name of the checkpoint.
        num_classes
            The number of classes. 1 for a regression task.
        pretrained
            Whether using the pretrained weights. If pretrained=True, download the pretrained model.
        tokenizer_name
            Name of the huggingface tokenizer type.
        """
        super().__init__()
        logger.debug(f"initializing {prefix} (CLIPForImageText)")
        logger.debug(f"model checkpoint: {checkpoint_name}")
        self.checkpoint_name = checkpoint_name
        self.num_classes = num_classes
        if is_matching:  # init both image and text attributes for matching
            has_image, has_text = True, True
        self.has_image = has_image
        self.has_text = has_text

        self.config, self.model = get_hf_config_and_model(checkpoint_name=checkpoint_name, pretrained=pretrained)

        if not self.has_image:
            self.config.vision_config = None
            self.model.vision_model = None
            self.model.visual_projection = None

        if not self.has_text:
            self.config.text_config = None
            self.model.text_model = None
            self.model.text_projection = None

        self.out_features = self.model.config.projection_dim

        self.head = nn.Linear(self.out_features, num_classes) if num_classes else nn.Identity()
        self.head.apply(init_weights)

        self.prefix = prefix
        if has_image:
            self.image_size, self.image_mean, self.image_std = get_image_size_mean_std(
                model_name=self.prefix,
                config=self.model.vision_model.config,
                provided_size=image_size,
                provided_norm_type=image_norm,
                support_variable_input_size=False,
            )
            self.use_learnable_image = use_learnable_image
            if self.use_learnable_image:
                self.learnable_image = nn.Parameter(torch.zeros(image_chan_num, self.image_size, self.image_size))
                logger.debug("will use a learnable image to replace missing ones")
        if has_text:
            self.tokenizer_name = tokenizer_name
            self.tokenizer = get_pretrained_tokenizer(
                tokenizer_name=self.tokenizer_name,
                checkpoint_name=self.checkpoint_name,
            )
            self.max_text_len = get_text_token_max_len(
                provided_max_len=max_text_len,
                config=self.model.text_model.config,
                tokenizer=self.tokenizer,
                checkpoint_name=self.checkpoint_name,
            )
            self.text_segment_num = get_text_segment_num(
                config=self.model.text_model.config,
                provided_segment_num=text_segment_num,
                checkpoint_name=self.checkpoint_name,
            )

        self.name_to_id = self.get_layer_ids()
        self.head_layer_names = [n for n, layer_id in self.name_to_id.items() if layer_id == 0]

    @property
    def text_token_ids_key(self):
        return f"{self.prefix}_{TEXT_TOKEN_IDS}"

    @property
    def text_valid_length_key(self):
        return f"{self.prefix}_{TEXT_VALID_LENGTH}"

    @property
    def image_key(self):
        return f"{self.prefix}_{IMAGE}"

    @property
    def image_valid_num_key(self):
        return f"{self.prefix}_{IMAGE_VALID_NUM}"

    @property
    def label_key(self):
        return f"{self.prefix}_{LABEL}"

    @property
    def text_column_prefix(self):
        return f"{self.text_token_ids_key}_{COLUMN}"

    @property
    def image_column_prefix(self):
        return f"{self.image_key}_{COLUMN}"

    @property
    def text_feature_dim(self):
        return self.model.config.text_config.hidden_size

    @property
    def image_feature_dim(self):
        return self.model.config.vision_config.hidden_size

    @property
    def input_keys(self):
        ret = []
        if self.has_image:
            ret.extend([self.image_key, self.image_valid_num_key])
        if self.has_text:
            ret.extend([self.text_token_ids_key, self.text_valid_length_key])
        return ret

    def forward(
        self,
        batch: dict,
    ):
        """
        Parameters
        ----------
        batch
            A dictionary containing the input mini-batch data.
            We need to use the keys with the model prefix to index required data.

        Returns
        -------
            A dictionary with logits and features.
        """
        has_image = self.has_image and self.image_key in batch
        has_text = self.has_text and self.text_token_ids_key in batch
        ret = {COLUMN_FEATURES: {FEATURES: {}, MASKS: {}}}

        if has_image:
            images = batch[self.image_key]
            image_valid_num = batch[self.image_valid_num_key]
            assert images.dim() == 5
            b, n, c, h, w = images.shape
            steps = torch.arange(0, n).type_as(image_valid_num)
            image_masks = steps.reshape((1, -1)) < image_valid_num.reshape((-1, 1))  # (b, n)
            if self.use_learnable_image:
                images = replace_missing_images_with_learnable(
                    images=images,
                    image_masks=image_masks,
                    learnable_image=self.learnable_image,
                )
            vision_outputs = self.model.vision_model(
                pixel_values=images.reshape((b * n, c, h, w)),
                output_attentions=True,
                output_hidden_states=True,
                return_dict=True,
            )
            image_features = self.model.visual_projection(vision_outputs.pooler_output)
            image_features = image_features.reshape((b, n, -1))  # (b, n, num_features)
            if not self.use_learnable_image:
                image_features = image_features * image_masks[:, :, None].type_as(image_features)

            # normalized features
            image_features = image_features / torch.clamp(image_features.norm(dim=-1, keepdim=True), min=1e-6)

            # collect image features by image column names
            image_column_features, image_column_feature_masks = get_column_features(
                batch=batch,
                column_name_prefix=self.image_column_prefix,
                features=image_features,
                valid_lengths=image_valid_num,
            )
            ret[COLUMN_FEATURES][FEATURES].update(image_column_features)
            ret[COLUMN_FEATURES][MASKS].update(image_column_feature_masks)

            image_features = image_features.mean(dim=1)  # (b, num_features)
            ret[FEATURES] = image_features

        if has_text:
            text_token_ids = batch[self.text_token_ids_key]
            text_valid_length = batch[self.text_valid_length_key]
            steps = torch.arange(0, text_token_ids.shape[1]).type_as(text_valid_length)
            text_masks = (steps.reshape((1, -1)) < text_valid_length.reshape((-1, 1))).type_as(text_token_ids)
            assert torch.equal(text_valid_length, text_masks.sum(dim=-1))

            text_outputs = self.model.text_model(
                input_ids=text_token_ids,
                attention_mask=text_masks,
                output_attentions=True,
                output_hidden_states=True,
                return_dict=True,
            )
            text_features = self.model.text_projection(text_outputs.pooler_output)  # (b, num_features)

            # normalized features
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)

            # collect text features by text column names
            text_column_features, text_column_feature_masks = get_column_features(
                batch=batch,
                column_name_prefix=self.text_column_prefix,
                features=self.model.text_projection(text_outputs.last_hidden_state),
                valid_lengths=text_valid_length,
                cls_feature=text_features,
            )
            ret[COLUMN_FEATURES][FEATURES].update(text_column_features)
            ret[COLUMN_FEATURES][MASKS].update(text_column_feature_masks)
            ret[FEATURES] = text_features

        if self.num_classes:
            if has_image and has_text:
                features = image_features + text_features
                logits = self.head(features)
                ret[FEATURES] = features
            elif has_image:
                logits = self.head(image_features)
            elif has_text:
                logits = self.head(text_features)
            else:
                raise RuntimeError("Neither image or text are used. Must have at least one.")
            ret[LOGITS] = logits
        else:
            ret[LOGIT_SCALE] = self.model.logit_scale.exp()
            if has_image and has_text:
                # cosine similarity as logits
                logits = torch.sum(image_features * text_features, dim=-1)
                ret[LOGITS] = logits

        return {self.prefix: ret}

    def get_layer_ids(
        self,
    ):
        """
        Assign an id to each layer. Layer ids will be used in layer-wise lr decay.
        Basically, id gradually increases when going from the output end to
        the input end. The layers defined in this class, e.g., head, have id 0.

        Returns
        -------
        A dictionary mapping the layer names (keys) to their ids (values).
        """
        model_prefixes = ["model.text_model", "model.vision_model", "model"]
        # later model prefixes can't starts with the early ones
        for i, model_pre in enumerate(model_prefixes):
            for model_pre2 in model_prefixes[i + 1 :]:
                if model_pre2.startswith(model_pre):
                    raise ValueError(
                        f"{model_pre} is a substring of {model_pre2}. Need to swap them in {model_prefixes}."
                    )

        pre_encoder_patterns = ("embeddings", "pre")
        post_encoder_patterns = ("head", "final", "post", "logit", "project")
        names = [n for n, _ in self.named_parameters()]

        name_to_id = {}
        for per_prefix in model_prefixes:
            per_model_name_to_id, names = assign_layer_ids(
                names=names,
                pre_encoder_patterns=pre_encoder_patterns,
                post_encoder_patterns=post_encoder_patterns,
                model_pre=per_prefix,
            )
            name_to_id.update(per_model_name_to_id)

        if len(names) > 0:
            logger.debug(f"outer layers are treated as head: {names}")
        for n in names:
            assert n not in name_to_id
            name_to_id[n] = 0

        return name_to_id
