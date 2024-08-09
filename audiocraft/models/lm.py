# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from dataclasses import dataclass
from functools import partial
import logging
import math
import typing as tp

from einops import rearrange
import torch
from torch import nn

from ..utils import utils
from ..utils.autocast import TorchAutocast
from ..conditioners.base import (
    ClassifierFreeGuidanceDropout, AttributeDropout,
    ConditionProvider, ConditionFuser,
    ConditionAttributes, ConditionType)
from ..modules.streaming import StreamingModule, State
from ..modules.transformer import StreamingTransformer, create_norm_fn, set_attention_context


logger = logging.getLogger(__name__)
ConditionTensors = tp.Dict[str, ConditionType]
CFGConditions = tp.Union[ConditionTensors, tp.Tuple[ConditionTensors, ConditionTensors]]


def get_init_fn(method: str, input_dim: int, init_depth: tp.Optional[int] = None):
    """LM layer initialization.
    Inspired from xlformers: https://github.com/fairinternal/xlformers

    Args:
        method (str): Method name for init function. Valid options are:
            'gaussian', 'uniform'.
        input_dim (int): Input dimension of the initialized module.
        init_depth (int, optional): Optional init depth value used to rescale
            the standard deviation if defined.
    """
    # Compute std
    std = 1 / math.sqrt(input_dim)
    # Rescale with depth
    if init_depth is not None:
        std = std / math.sqrt(2 * init_depth)

    if method == 'gaussian':
        return partial(
            torch.nn.init.trunc_normal_, mean=0.0, std=std, a=-3 * std, b=3 * std
        )
    elif method == 'uniform':
        bound = math.sqrt(3) * std  # ensure the standard deviation is `std`
        return partial(torch.nn.init.uniform_, a=-bound, b=bound)
    else:
        raise ValueError("Unsupported layer initialization method")


def init_layer(m: nn.Module,
               method: str,
               init_depth: tp.Optional[int] = None,
               zero_bias_init: bool = False):
    """Wrapper around ``get_init_fn`` for proper initialization of LM modules.

    Args:
        m (nn.Module): Module to initialize.
        method (str): Method name for the init function.
        init_depth (int, optional): Optional init depth value used to rescale
            the standard deviation if defined.
        zero_bias_init (bool): Whether to initialize the bias to 0 or not.
    """
    if isinstance(m, nn.Linear):
        init_fn = get_init_fn(method, m.in_features, init_depth=init_depth)
        if m.weight.device.type == 'cpu' and m.weight.dtype == torch.float16:
            weight = m.weight.float()
            init_fn(weight)
            m.weight.data[:] = weight.half()
        else:
            init_fn(m.weight)
        if zero_bias_init and m.bias is not None:
            nn.init.constant_(m.bias, 0)
    elif isinstance(m, nn.Embedding):
        init_fn = get_init_fn(method, m.embedding_dim, init_depth=None)
        if m.weight.device.type == 'cpu' and m.weight.dtype == torch.float16:
            weight = m.weight.float()
            init_fn(weight)
            m.weight.data[:] = weight.half()
        else:
            init_fn(m.weight)


class ScaledLinear(nn.Linear):
    """Boost learning rate for linear layer (with `scale`).

    Args:
        lr (float or None): Learning rate for the linear layer if provided.
    """
    def __init__(self, *args, lr=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.lr = lr

    def make_optim_group(self):
        group = {"params": list(self.parameters())}
        if self.lr is not None:
            group["lr"] = self.lr
        return group


class ScaledEmbedding(nn.Embedding):
    """Boost learning rate for embeddings (with `scale`).

    Args:
        lr (float or None): Learning rate for the embedding layer if provided.
        norm (bool): if True, uses a layer norm after the embedding.
        zero_idx (int): special value indicating that the output should be exactly 0.
    """
    def __init__(self, *args, lr=None, norm: bool = False,
                 zero_idx: int = -1, **kwargs):
        super().__init__(*args, **kwargs)
        self.lr = lr
        self.norm = None
        if norm:
            self.norm = create_norm_fn('layer_norm', self.embedding_dim)
        assert zero_idx < 0, 'Please use negative values for the zero_idx.'
        self.zero_idx = zero_idx

    def make_optim_group(self):
        group = {"params": list(self.parameters())}
        if self.lr is not None:
            group["lr"] = self.lr
        return group

    def forward(self, input, *args, **kwargs):
        is_zero = input == self.zero_idx
        zero = torch.zeros(1, dtype=input.dtype, device=input.device)
        input = input.clamp(min=0)
        y = super().forward(input, *args, **kwargs)
        if self.norm is not None:
            y = self.norm(y)
        y = torch.where(is_zero[..., None], zero, y)
        return y


def _delay_sequence(delays: tp.List[int], tensor: torch.Tensor, padding: torch.Tensor) -> torch.Tensor:
    B, K, T = tensor.shape
    assert len(delays) == K, (len(delays), K)
    outs = []

    for k, delay in enumerate(delays):
        assert delay >= 0
        line = tensor[:, k].roll(delay, dims=1)
        if delay > 0:
            line[:, :delay] = padding[:, k]
        outs.append(line)
    return torch.stack(outs, dim=1)


def _undelay_sequence(delays: tp.List[int], tensor: torch.Tensor,
                      fill_value: tp.Union[int, float] = float('NaN')) -> tp.Tuple[torch.Tensor, torch.Tensor]:
    B, K, T, *_ = tensor.shape
    assert len(delays) == K
    mask = torch.ones(B, K, T, dtype=torch.bool, device=tensor.device)
    outs = []
    if all([delay == 0 for delay in delays]):
        return tensor, mask
    for k, delay in enumerate(delays):
        assert delay >= 0
        line = tensor[:, k].roll(-delay, dims=1)
        if delay > 0:
            line[:, -delay:] = fill_value
            mask[:, k, -delay:] = 0
        outs.append(line)
    return torch.stack(outs, dim=1), mask


@dataclass
class LMOutput:
    # The logits are already re-aligned with the input codes
    # hence no extra shift is required, e.g. when computing CE
    logits: tp.Optional[torch.Tensor]  # [B, K, T, card]
    mask: tp.Optional[torch.Tensor]  # [B, K, T]
    text_logits: tp.Optional[torch.Tensor]  # [B, 1, T, text_card]
    text_mask: tp.Optional[torch.Tensor]  # [B, 1, T]


class LMModel(StreamingModule):
    """Transformer-based language model on multiple streams of codes.

    Args:
        condition_provider (MusicConditionProvider): Conditioning provider from metadata.
        fuser (ConditionFuser): Fuser handling the fusing of conditions with language model input.
        n_q (int): Number of parallel streams to model.
        card (int): Cardinality, vocabulary size.
        text_card (int): Cardinality of the text vocabulary. Activates text support
        dim (int): Dimension of the transformer encoder.
        num_heads (int): Number of heads for the transformer encoder.
        hidden_scale (int): Scale for hidden feed forward dimension of the transformer encoder.
        norm (str): Normalization method.
        norm_emb (bool): Whether to normalize embeddings.
        emb_lr (float, optional): Embedding-specific learning rate.
        bias_proj (bool): Use bias for output projections.
        weight_init (str, optional): Method for weight initialization.
        depthwise_init (str, optional): Method for depthwise weight initialization.
        zero_bias_init (bool): If true and bias in Linears, initialize bias to zeros.
        cfg_dropout (float): Classifier-free guidance dropout.
        cfg_coef (float): Classifier-free guidance coefficient.
        attribute_dropouts (dict): Attribute dropout probabilities.
        two_step_cfg (bool): Whether to run classifier free-guidance with 2 distinct steps.
        depformer (bool): whether to use a smaller Transformer along the codebooks for predicting them.
        depformer_*: params used for the Depformer Transformer, all the other will be shared.
        depformer_multi_linear (bool): if True, uses one linear layer per codebook to project the
            output of the main transformer to the Depformer latent space.
        depformer_dim_feedforward (int| list[int]| None): If None, defaults to hidden_scale * depformer_dim.
        repeat_penalty_coef (float): amount of penalty to apply to the logits of the first codebook.
            Typically, if a codebook entry was repeat non stop, then the max penalty in probability space
            would be `exp(repeat_penalty_coef)`.
        repeat_penalty_length (float): the repeat penalty coef gets multiplied by the EMA of the one hot encoding
            of the codebook picked by the model. The weight for changing the EMA is `1 / repeat_penalty_length`.
        autocast (TorchAutocast): autocast to use when evaluating the LM. This is better than
            wrapping calls to the LMModel with autocast, as this allows to exclude the conditioning
            computation.
        existing_text_padding_id (bool): if True, will use a different token for the initial text token, and
            the text padding token.
        text_context (int or None): value of the attention context span when in text only mode. If None,
            text will have an infinite context.
        same_initial (bool): if True, uses the same initial tokens for both text and audio mode.
        **kwargs: Additional parameters for the transformer encoder.
    """
    def __init__(self, condition_provider: ConditionProvider, fuser: ConditionFuser,
                 delays: tp.List[int] = [0],
                 n_q: int = 8, card: int = 1024, text_card: tp.Optional[int] = None,
                 dim: int = 128, num_heads: int = 8,
                 hidden_scale: int = 4, norm: str = 'layer_norm',
                 norm_emb: bool = False, emb_lr: tp.Optional[float] = None, transformer_lr: tp.Optional[float] = None,
                 text_lr: tp.Optional[float] = None, bias_proj: bool = False, weight_init: tp.Optional[str] = None,
                 depthwise_init: tp.Optional[str] = None, zero_bias_init: bool = False, cfg_dropout: float = 0,
                 cfg_coef: float = 1.0,
                 attribute_dropouts: tp.Dict[str, float] = {}, two_step_cfg: bool = False,
                 depformer: bool = False, depformer_dim: int = 256,
                 depformer_dim_feedforward: int | list[int] | None = None,
                 depformer_multi_linear: bool = False,
                 depformer_weights_per_step: bool = False, depformer_pos_emb: str = 'sin',
                 repeat_penalty_coef: float = 0., repeat_penalty_length: float = 4,
                 autocast: TorchAutocast = TorchAutocast(enabled=False),
                 existing_text_padding_id: tp.Optional[int] = None, context: tp.Optional[int] = None,
                 text_context: tp.Optional[int] = None, same_initial: bool = False,
                 device=None, dtype=None, **kwargs):
        super().__init__()
        self.condition_provider = condition_provider
        self.fuser = fuser
        self.n_q = n_q
        self.card = card
        self.text_card = text_card
        assert len(delays) > 0, "Delays must be non empty"
        assert len(delays) <= self.num_codebooks, "Too many delays"
        if len(delays) < self.num_codebooks:
            delays = delays + [delays[-1]] * (self.num_codebooks - len(delays))
            logger.info("Extended delay to %r", delays)
        self.delays = delays
        self.cfg_coef = cfg_coef
        self.cfg_dropout = ClassifierFreeGuidanceDropout(p=cfg_dropout)
        self.att_dropout = AttributeDropout(dropouts=attribute_dropouts)
        self.dim = dim
        self.two_step_cfg = two_step_cfg
        self.repeat_penalty_coef = repeat_penalty_coef
        self.repeat_penalty_length = repeat_penalty_length
        self.existing_text_padding_id = existing_text_padding_id
        self.context = context
        self.text_context = text_context
        self.same_initial = same_initial
        self.autocast = autocast
        kwargs['context'] = context
        EmbeddingFactory = partial(ScaledEmbedding, norm=norm_emb, device=device, lr=emb_lr, dtype=dtype,
                                   zero_idx=self.zero_token_id)
        self.emb = nn.ModuleList([EmbeddingFactory(self.card + 1, dim) for _ in range(n_q)])
        if text_card:
            # Text card + padding token (if not in the original tokenizer)
            extra_text = self.existing_text_padding_id is None
            # Unlike for audio, here we authorize the model to output the special token.
            self.text_linear: tp.Union[nn.Linear, ScaledLinear]
            if text_lr is not None:
                # We also have the 'initial' token in the embedding table, but not in the linear.
                self.text_emb = EmbeddingFactory(text_card + 1, dim, lr=text_lr)
                self.text_linear = ScaledLinear(dim, text_card + extra_text, bias=bias_proj, lr=text_lr)
            else:
                self.text_emb = EmbeddingFactory(text_card + 1, dim)
                self.text_linear = nn.Linear(dim, text_card + extra_text, bias=bias_proj)
        depformer_prefix = 'depformer_'
        main_kwargs = {k: v for k, v in kwargs.items() if not k.startswith(depformer_prefix)}
        self.transformer = StreamingTransformer(
            d_model=dim, num_heads=num_heads, dim_feedforward=int(hidden_scale * dim), norm=norm,
            device=device, dtype=dtype, lr=transformer_lr, **main_kwargs)
        self.out_norm = create_norm_fn(norm, dim)
        self.depformer: tp.Optional[nn.Module] = None
        self.depformer_multi_linear = depformer_multi_linear
        if depformer:
            kwargs_dep = main_kwargs.copy()
            kwargs_dep.update({
                k.removeprefix(depformer_prefix): v for k, v in kwargs.items() if k.startswith(depformer_prefix)
            })
            kwargs_dep['positional_embedding'] = depformer_pos_emb
            kwargs_dep['cross_attention'] = False
            kwargs_dep['context'] = None
            if depformer_weights_per_step:
                kwargs_dep['weights_per_step'] = n_q
            if depformer_multi_linear:
                # One linear layer per codebook to project different informations from the main model.
                self.depformer_in = nn.ModuleList([nn.Linear(dim, depformer_dim, bias=False) for _ in range(n_q)])
            else:
                self.depformer_in = nn.ModuleList([nn.Linear(dim, depformer_dim, bias=False)])
            # Only using up to n_q - 1 because the last codebook is never an input to Depformer.
            self.depformer_emb = nn.ModuleList([
                EmbeddingFactory(self.card + 1, depformer_dim) for _ in range(n_q - 1)])
            if text_card is not None:
                self.depformer_text_emb = EmbeddingFactory(text_card + 1, depformer_dim)
            if depformer_dim_feedforward is None:
                depformer_dim_feedforward = int(hidden_scale * depformer_dim)
            self.depformer = StreamingTransformer(
                d_model=depformer_dim, dim_feedforward=depformer_dim_feedforward, norm=norm, device=device,
                dtype=dtype, **kwargs_dep,
            )
            dim = depformer_dim  # we will directly apply the next linears to the output of the Depformer.

        self.linears = nn.ModuleList([nn.Linear(dim, self.card, bias=bias_proj)
                                      for _ in range(n_q)])
        self._init_weights(weight_init, depthwise_init, zero_bias_init)
        self._fsdp: tp.Optional[nn.Module]
        self.__dict__['_fsdp'] = None

    def _init_weights(self, weight_init: tp.Optional[str], depthwise_init: tp.Optional[str], zero_bias_init: bool):
        """Initialization of the transformer module weights.

        Args:
            weight_init (str, optional): Weight initialization strategy. See ``get_init_fn`` for valid options.
            depthwise_init (str, optional): Depthwise initialization strategy. The following options are valid:
                'current' where the depth corresponds to the current layer index or 'global' where the total number
                of layer is used as depth. If not set, no depthwise initialization strategy is used.
            zero_bias_init (bool): Whether to initialize bias to zero or not.
        """
        assert depthwise_init is None or depthwise_init in ['current', 'global']
        assert depthwise_init is None or weight_init is not None, \
            "If 'depthwise_init' is defined, a 'weight_init' method should be provided."
        assert not zero_bias_init or weight_init is not None, \
            "If 'zero_bias_init', a 'weight_init' method should be provided"

        if weight_init is None:
            return

        for emb_layer in self.emb:
            init_layer(emb_layer, method=weight_init, init_depth=None, zero_bias_init=zero_bias_init)
        if self.depformer is not None:
            for emb_layer in self.depformer_emb:
                init_layer(emb_layer, method=weight_init, init_depth=None, zero_bias_init=zero_bias_init)
        if self.has_text:
            init_layer(self.text_emb, method=weight_init, init_depth=None, zero_bias_init=zero_bias_init)
            if self.depformer is not None:
                init_layer(self.depformer_text_emb, method=weight_init, init_depth=None,
                           zero_bias_init=zero_bias_init)
            init_layer(self.text_linear, method=weight_init, init_depth=None, zero_bias_init=zero_bias_init)

        for layer_idx, tr_layer in enumerate(self.transformer.layers):
            depth = None
            if depthwise_init == 'current':
                depth = layer_idx + 1
            elif depthwise_init == 'global':
                depth = len(self.transformer.layers)
            init_fn = partial(init_layer, method=weight_init, init_depth=depth, zero_bias_init=zero_bias_init)
            tr_layer.apply(init_fn)

        for linear in self.linears:
            init_layer(linear, method=weight_init, init_depth=None, zero_bias_init=zero_bias_init)

    @property
    def initial_token_id(self) -> int:
        """Token id for the start of sequence (audio)."""
        return self.card

    @property
    def text_initial_token_id(self) -> int:
        """Token id for the start of sequence (text)."""
        assert self.text_card is not None
        return self.text_card

    @property
    def text_padding_token_id(self) -> int:
        """Token id for text padding."""
        assert self.text_card is not None
        if self.existing_text_padding_id is None:
            return self.text_card
        else:
            return self.existing_text_padding_id

    @property
    def end_of_text_padding_id(self) -> int:
        """Token id for optionally marking the last padding step for a word."""
        return 0

    @property
    def zero_token_id(self) -> int:
        """Special value in the input tokens, indicating that no sampling should
        happen for that value, and no input should be given to the model."""
        return -1

    @property
    def ungenerated_token_id(self) -> int:
        """Special value that can be provided in the prompt to indicate that this specific
        value should be predicted and sampled. This allows for partial teacher forcing, by generating
        one modality, with the other one fixed.
        """
        return -2

    @property
    def num_codebooks(self) -> int:
        return self.n_q + int(self.has_text)

    @property
    def num_audio_codebooks(self) -> int:
        return self.n_q

    @property
    def audio_offset(self) -> int:
        return int(self.has_text)

    @property
    def has_text(self) -> bool:
        return self.text_card is not None

    def _get_initial_token(self, text_or_audio: str = 'audio') -> torch.Tensor:
        # Returns the initial token that will be fed to the model to predict the very first timestep.
        # The output shape will be [B, K, 1].
        device = next(iter(self.parameters())).device
        zero = torch.full([1, 1, 1], self.zero_token_id, device=device, dtype=torch.long)
        special = torch.full_like(zero, self.initial_token_id)

        if not self.has_text:
            assert text_or_audio == 'audio'
            return special.expand(-1, self.num_audio_codebooks, -1)

        text_special = torch.full_like(zero, self.text_initial_token_id)
        if self.same_initial or text_or_audio == 'both':
            audio_token = special
            text_token = text_special
        elif text_or_audio == 'text':
            text_token = text_special
            audio_token = zero
        elif text_or_audio == 'audio':
            text_token = zero
            audio_token = special
        else:
            raise ValueError(f"Invalid value for `text_or_audio`: {text_or_audio}")

        audio_token = audio_token.expand(-1, self.num_audio_codebooks, -1)
        token = torch.cat([text_token, audio_token], dim=1)
        return token

    def forward(self, sequence: torch.Tensor,
                conditions: tp.List[ConditionAttributes],
                condition_tensors: tp.Optional[ConditionTensors] = None,
                text_or_audio: str = 'audio') -> tp.Tuple[tp.Optional[torch.Tensor], tp.Optional[torch.Tensor]]:
        """Apply language model on sequence and conditions.
        Given a tensor of sequence of shape [B, Kt + Ka, S] with `Kt = 1` if text is supported, `0` otherwise,
        and `Ka` the number of audio codebooks, S the sequence steps.
        Returns a tuple with either the audio logits or the text logits, or both.

        ..Important:: The number of codebooks will be `1 + num_audio_codebooks` when text is supported.
            The first 'codebook' is then the text. It must always be provided, potentially filled with
            `self.zero_token_id`, even when `text_or_audio='text'`.

        Args:
            sequence (torch.Tensor): Tokens to model.
            conditions (list of ConditioningAttributes): Conditions to use when modeling
                the given codes. Note that when evaluating multiple time with the same conditioning
                you should pre-compute those and pass them as `condition_tensors`.
            condition_tensors (dict[str, ConditionType], optional): Pre-computed conditioning
                tensors, see `conditions`.
            text_or_audio (str): controls whether to generate only text, audio, or both.
        Returns:
            torch.Tensor or None: audio logits when audio is generated. Shape `[B, Ka, S, card]`
            torch.Tensor or None: text logits when `model.has_text` is true and text is generated.
                Shape `[B, 1, S, text_card]`.
        """
        transformer_out = None
        audio_offset = int(self.has_text)
        logits: tp.Optional[torch.Tensor] = None
        text_logits: tp.Optional[torch.Tensor] = None
        assert text_or_audio in {'text', 'audio', 'both'}
        gen_text = text_or_audio in {'text', 'both'}
        gen_audio = text_or_audio in {'audio', 'both'}
        if text_or_audio in {'text', 'both'}:
            assert self.has_text, "`text_audio_only in {'text', 'both'}` doesn't make sense for an audio only model."

        Ka = self.num_audio_codebooks
        B, K, S = sequence.shape
        if 'transformer_out' in self._streaming_state:
            assert self.depformer is not None
            assert K == 1, f"Codebooks for Depformer streaming should be passed 1 by 1, got {K}."
            # We are in the middle of a depformer eval and do not need to evaluate the
            # main transformer model.
            transformer_out = self._streaming_state['transformer_out']
        else:
            assert K == self.num_codebooks, f"Sequence shape {sequence.shape} must match the number of codebooks."
            input_sequence = sequence
            if not self._is_streaming:
                # When not streaming, the last step cannot be evaluated, and is provided
                # only for the supervision of the Depformer. Thus we ignore it here.
                input_sequence = sequence[:, :, :-1]
            input_ = None
            if gen_audio:
                for cb_index in range(self.num_audio_codebooks):
                    audio_emb = self.emb[cb_index](input_sequence[:, cb_index + self.audio_offset])
                    input_ = audio_emb if input_ is None else input_ + audio_emb
            if gen_text:
                text_emb = self.text_emb(input_sequence[:, 0])
                input_ = text_emb if input_ is None else input_ + text_emb
            assert input_ is not None

            if condition_tensors is None:
                assert not self._is_streaming, "Conditions tensors should be precomputed when streaming."
                # apply dropout modules
                conditions = self.cfg_dropout(conditions)
                conditions = self.att_dropout(conditions)
                prepared = self.condition_provider.prepare(conditions)
                # encode conditions and fuse, both have a streaming cache to not recompute when generating.
                condition_tensors = self.condition_provider(prepared)
            else:
                assert not conditions, "Shouldn't pass both conditions and condition_tensors."

            input_, cross_attention_input = self.fuser(input_, condition_tensors)
            transformer_out = self.transformer(input_, cross_attention_src=cross_attention_input)
            if self.out_norm:
                transformer_out = self.out_norm(transformer_out)
            # remove the prefix from the model outputs
            if len(self.fuser.fuse2cond['prepend']) > 0:
                assert transformer_out is not None
                transformer_out = transformer_out[:, -input_sequence.shape[2]:]

        assert isinstance(transformer_out, torch.Tensor)

        if self.depformer is None:
            if gen_text:
                text_logits = self.text_linear(transformer_out)
            if gen_audio:
                logits_list = [self.linears[k](transformer_out) for k in range(Ka)]
                logits = torch.stack(logits_list, dim=1)  # [B, K, S, card]
        else:
            if self._is_streaming:
                # When streaming with Depformer, there will be a number of call to `forward`:
                # - First call for a time step, `transformer_out` is computed.
                #   > if text is needed, the text token logits are returned.
                #   > otherwise, the first codebook audio logits are returned.
                # - if audio is NOT generated, we can stop here. Otherwise we populate the streaming state.
                # - Then, `transformer_out` is read from the streaming state.
                #   > if the text is generated, we now return the first codebook audio logits.
                #   > otherwise, we are now computing the second codebook logits.
                # - Iterate until we have generated all the audio codebooks.
                # - For the last audio codebook, we wipe out the streaming state of the depformer.
                assert not self.training
                depformer_first_call = False
                if 'transformer_out' in self._streaming_state:
                    assert transformer_out.shape[1] == 1, transformer_out.shape
                else:
                    # Depformer doesn't care about past latent space of the transformers, in particular for the prompt.
                    # We only need the timestep for which we need to provide a prediction.
                    transformer_out = transformer_out[:, -1:]
                    depformer_first_call = True
                depformer_cb_index = self._streaming_state.get('depformer_cb_index', 0)
                depformer_was_audio = False
                # Let's make the typer happy, normally StreamingState is a dict of tensors.
                assert isinstance(depformer_cb_index, int)
                assert isinstance(transformer_out, torch.Tensor)  # typer is really stupid
                last_token_input: tp.Optional[torch.Tensor] = None
                if gen_text and depformer_first_call:
                    text_logits = self.text_linear(transformer_out)
                else:
                    assert gen_audio, "this should never get called unless we want audio generated."
                    depformer_was_audio = True
                    depformer_input = transformer_out
                    if self.depformer_multi_linear:
                        depformer_input = self.depformer_in[depformer_cb_index](depformer_input)
                    else:
                        depformer_input = self.depformer_in[0](depformer_input)
                    if self.has_text and depformer_cb_index == 0:
                        if gen_text:
                            # There was a first call to generate the text token which we can
                            # now use as conditioning.
                            last_token_input = self.depformer_text_emb(sequence[:, 0])
                    elif depformer_cb_index > 0:
                        assert sequence.shape[2] == 1
                        assert 'transformer_out' in self._streaming_state
                        # sequence is [B, 1, 1]
                        last_token_input = self.depformer_emb[depformer_cb_index - 1](sequence[:, 0])
                    if last_token_input is not None:
                        depformer_input = depformer_input + last_token_input
                    assert depformer_input.shape[1] == 1
                    # depformer_input is [B, 1, depformer_dim].
                    dep_output = self.depformer(depformer_input)
                    logits = self.linears[depformer_cb_index](dep_output)
                    assert logits is not None
                    logits = logits[:, None]
                    # Now logits is [B, 1, 1, card]
                if gen_audio:
                    if depformer_was_audio and depformer_cb_index == Ka - 1:
                        self.depformer.reset_streaming()
                        self._streaming_state.pop('depformer_cb_index', None)
                        self._streaming_state.pop('transformer_out', None)
                    else:
                        # We only need to store the depformer state when generating audio, otherwise
                        # there will never be more than one call per time step.
                        self._streaming_state['transformer_out'] = transformer_out
                        if depformer_was_audio:
                            self._streaming_state['depformer_cb_index'] = depformer_cb_index + 1  # type: ignore
            else:
                # teacher forcing evaluation of the depformer transformer.
                if gen_text:
                    text_logits = self.text_linear(transformer_out)
                if gen_audio:
                    if self.depformer_multi_linear:
                        depformer_input = torch.stack([self.depformer_in[k](transformer_out) for k in range(Ka)])
                    else:
                        depformer_input = self.depformer_in[0](transformer_out)[None]
                    if gen_text:
                        text_input = self.depformer_text_emb(sequence[:, 0, 1:])
                        depformer_inputs = [text_input]
                    else:
                        depformer_inputs = [torch.zeros_like(depformer_input[0])]

                    depformer_inputs += [
                        self.depformer_emb[k](sequence[:, k + audio_offset, 1:]) for k in range(Ka - 1)]

                    depformer_input = depformer_input + torch.stack(depformer_inputs, dim=0)
                    # Now depformer_input is [Ka, B, S, depformer_dim].
                    depformer_input = rearrange(depformer_input, 'k b s d -> (b s) k d')
                    depformer_output = self.depformer(depformer_input)
                    depformer_output = rearrange(depformer_output, '(b s) k d -> k b s d', b=B)
                    logits_list = [self.linears[k](depformer_output[k]) for k in range(Ka)]
                    logits = torch.stack(logits_list, dim=1)  # [B, Ka, S - 1, card]

        if logits is not None:
            assert logits.dim() == 4, logits.shape  # [B, Ka, S, card]
        if text_logits is not None:
            text_logits = text_logits[:, None]
            assert text_logits.dim() == 4, text_logits.shape  # [B, 1, S, card]
        assert logits is not None or text_logits is not None
        return logits, text_logits

    def compute_predictions(
            self, codes: torch.Tensor,
            conditions: tp.List[ConditionAttributes],
            condition_tensors: tp.Optional[ConditionTensors] = None,
            text_or_audio: str = 'audio') -> LMOutput:
        """Given an input tensor of codes [B, K, T] and list of conditions, runs the model
        forward using the specified codes interleaving pattern.

        Args:
            codes (torch.Tensor): Input codes of shape [B, K, T] with B the batch size,
                K the number of codebooks and T the number of timesteps. When text is supported,
                the first 'codebook' corresponds to the text, and the remaining codebooks are for the  audio.
            conditions (list of ConditioningAttributes): conditionings to use when modeling
                the given codes. Note that when evaluating multiple time with the same conditioning
                you should pre-compute those and pass them as `condition_tensors`.
            condition_tensors (dict[str, ConditionType], optional): pre-computed conditioning
                tensors, see `conditions`.
            text_or_audio (str): one of 'text', 'audio' or 'both' to control whether to generate
                text, audio, or both.
        Returns:
            LMOutput: Language model outputs, containing either text or audio logits, or both.
                logits (torch.Tensor, or None) of shape [B, K, T, card] corresponding to the provided codes,
                    i.e. the first item corresponds to logits to predict the first code, meaning that
                    no additional shifting of codes and logits is required.
                mask (torch.Tensor, or None) of shape [B, K, T], mask over valid and invalid positions.
                    Given the specified interleaving strategies, parts of the logits and codes should
                    not be considered as valid predictions because of invalid context.
                text_logits (torch.Tensor, or None) of shape [B, 1, T, text_card].
                text_mask (torch.Tensor, or None) of shape [B, 1, T], mask over the valid positions for the text.
        """
        B, K, T = codes.shape
        assert K == self.num_codebooks, (K, self.num_codebooks)
        # Delaying codes and removing the last time step that will never be an input.
        initial = self._get_initial_token(text_or_audio).expand(B, -1, -1)
        delayed_codes = _delay_sequence(self.delays, codes, initial)
        # Inserting the empty tokens for the first time step.
        delayed_codes = torch.cat([initial, delayed_codes], dim=2)

        # apply model on pattern sequence
        model = self if self._fsdp is None else self._fsdp
        if condition_tensors is None:
            assert len(conditions) == B, f"Wrong number of condition with attributes {len(conditions)} != {B}"
            prepared = self.condition_provider.prepare(conditions)
            condition_tensors = self.condition_provider(prepared)

        context = self.text_context if text_or_audio == 'text' else self.context
        set_attention_context(self.transformer, context)
        with self.autocast:
            logits, text_logits = model(delayed_codes, [], condition_tensors, text_or_audio)  # [B, K, S, card]
        # map back the logits on pattern sequence to logits on original codes: [B, K, S, card] -> [B, K, T, card]
        # and provide the corresponding mask over invalid positions of tokens
        logits_mask = None
        if logits is not None:
            logits, logits_mask = _undelay_sequence(self.delays[self.audio_offset:], logits, fill_value=float('NaN'))
            logits_mask &= (codes[:, self.audio_offset:] != self.zero_token_id)
        text_logits_mask = None
        if text_logits is not None:
            text_logits, text_logits_mask = _undelay_sequence(self.delays[:1], text_logits, fill_value=float('NaN'))
            text_logits_mask &= (codes[:, :1] != self.zero_token_id)
        return LMOutput(logits, logits_mask, text_logits, text_logits_mask)

    def _sample_next_token(self,
                           sequence: torch.Tensor,
                           cfg_conditions: CFGConditions,
                           unconditional_state: State,
                           use_sampling: bool = False,
                           temp: float = 1.0,
                           top_k: int = 0,
                           top_p: float = 0.0,
                           cfg_coef: tp.Optional[float] = None,
                           two_step_cfg: tp.Optional[bool] = None,
                           text_or_audio: str = 'both'
                           ) -> tp.Tuple[torch.Tensor, tp.Tuple[tp.Optional[torch.Tensor], tp.Optional[torch.Tensor]]]:
        """Sample next token from the model given a sequence and a set of conditions. The model supports
        multiple sampling strategies (greedy sampling, softmax, top-k, top-p...).

        Args:
            sequence (torch.Tensor): Current sequence of shape [B, K, S]
                with K corresponding to the number of codebooks and S the number of sequence steps.
                S = 1 in streaming mode, except for the first step that contains a bigger prompt.
            cfg_conditions (CFGCondition): Set of conditions. Exact type will depend on whether CFG is used,
                and whether `two_step_cfg` is True or not.
            use_sampling (bool): Whether to use a sampling strategy or not.
            temp (float): Sampling temperature.
            top_k (int): K for "top-k" sampling.
            top_p (float): P for "top-p" sampling.
            cfg_coef (float, optional): classifier free guidance coefficient
            two_step_cfg (bool, optional): if True, does two forward passes to compute the
                logits for the classifier free guidance, otherwise,
                a single one with a batch size doubled (and some extra padding).
            text_or_audio (str): one of 'text', 'audio' or 'both', to control whether to generate
                text, audio, or both.
        Returns:
            next_token (torch.Tensor): Next token tensor of shape [B, K, 1].
        """
        B = sequence.shape[0]
        cfg_coef = self.cfg_coef if cfg_coef is None else cfg_coef
        model = self if self._fsdp is None else self._fsdp
        two_step_cfg = self.two_step_cfg if two_step_cfg is None else two_step_cfg
        kwargs = {'text_or_audio': text_or_audio}
        logits = None
        text_logits = None
        if cfg_coef == 1.:
            # We have conditioning but no CFG.
            assert isinstance(cfg_conditions, dict)
            condition_tensors = cfg_conditions
            logits, text_logits = model(sequence, conditions=[], condition_tensors=condition_tensors, **kwargs)
        else:
            # We have two versions, either with two forward pass, or with a single and
            # stacking the version with and without conditioning.
            if two_step_cfg:
                assert isinstance(cfg_conditions, tuple), type(cfg_conditions)
                condition_tensors, null_condition_tensors = cfg_conditions
                cond_logits, cond_text_logits = model(
                    sequence, conditions=[], condition_tensors=condition_tensors, **kwargs)
                state = self.get_streaming_state()
                self.set_streaming_state(unconditional_state)
                uncond_logits, uncond_text_logits = model(
                    sequence, conditions=[], condition_tensors=null_condition_tensors, **kwargs)
                unconditional_state.update(self.get_streaming_state())
                self.set_streaming_state(state)
                if uncond_logits is not None:
                    assert cond_logits is not None
                    logits = uncond_logits + (cond_logits - uncond_logits) * cfg_coef
                if uncond_text_logits is not None:
                    assert cond_text_logits is not None
                    text_logits = uncond_text_logits + (cond_text_logits - uncond_text_logits) * cfg_coef
            else:
                assert isinstance(cfg_conditions, dict)
                condition_tensors = cfg_conditions
                assert condition_tensors
                # repeating the token sequence.
                sequence = torch.cat([sequence, sequence], dim=0)
                all_logits, all_text_logits = model(
                    sequence, conditions=[],
                    condition_tensors=condition_tensors, **kwargs)
                if all_logits is not None:
                    cond_logits, uncond_logits = all_logits.split(B, dim=0)  # [B, K, T, card]
                    logits = uncond_logits + (cond_logits - uncond_logits) * cfg_coef
                if all_text_logits is not None:
                    cond_text_logits, uncond_text_logits = all_text_logits.split(B, dim=0)  # [B, K, T, card]
                    text_logits = uncond_text_logits + (cond_text_logits - uncond_text_logits) * cfg_coef

        # Repeat penalty for the first codebook.
        # When `logits` corresponds to the codebook 0, `depformer_cb_index` will already be updated to 1.
        counts: tp.Optional[torch.Tensor] = None
        is_first = self._streaming_state.get('depformer_cb_index') == 1
        if is_first and self.repeat_penalty_coef > 0.:
            assert logits is not None
            if 'counts' not in self._streaming_state:
                self._streaming_state['counts'] = torch.zeros(
                    B, 1, 1, self.card, dtype=torch.float, device=logits.device)
            counts = self._streaming_state['counts']
            logits = torch.log_softmax(logits, dim=-1)
            logits -= self.repeat_penalty_coef * counts

        tokens_per_modality = []
        # When using Depformer, only one of the logits will be active at any time.
        for modality_logits in [text_logits, logits]:
            if modality_logits is None:
                continue
            modality_logits = modality_logits[:, :, -1, :].float()  # [B, K, card]
            # Apply softmax for sampling if temp > 0. Else, do greedy sampling to avoid zero division error.
            if use_sampling and temp > 0.0:
                probs = torch.softmax(modality_logits / temp, dim=-1)
                if top_p > 0.0:
                    next_token = utils.sample_top_p(probs, p=top_p)
                elif top_k > 0:
                    next_token = utils.sample_top_k(probs, k=top_k)
                else:
                    next_token = utils.multinomial(probs, num_samples=1)
            else:
                next_token = torch.argmax(modality_logits, dim=-1, keepdim=True)
            tokens_per_modality.append(next_token)

        next_token = torch.cat(tokens_per_modality, dim=1)

        if is_first and self.repeat_penalty_coef > 1.0:
            # Keeping track of EMA of one hot encoding of the selected codebook entry.
            alpha = 1 / self.repeat_penalty_length
            if is_first:
                assert counts is not None
                one_hot = nn.functional.one_hot(next_token, self.card)
                counts[:] = counts * (1 - alpha) + alpha * one_hot

        assert next_token.dim() == 3, next_token.shape
        return next_token, (text_logits, logits)

    @torch.no_grad()
    def generate(self,
                 prompt: tp.Optional[torch.Tensor] = None,
                 conditions: tp.List[ConditionAttributes] = [],
                 num_samples: tp.Optional[int] = None,
                 max_gen_len: int = 256,
                 use_sampling: bool = True,
                 temp: float = 1.0,
                 top_k: int = 250,
                 top_p: float = 0.0,
                 strip: int = 0,
                 cfg_coef: tp.Optional[float] = None,
                 two_step_cfg: tp.Optional[bool] = None,
                 check: bool = False,
                 min_start_offset: tp.Optional[int] = None,
                 callback: tp.Optional[tp.Callable[[int, int], None]] = None,
                 postprocess_conditions: tp.Optional[tp.Callable[[CFGConditions], CFGConditions]] = None,
                 get_null_conditions: tp.Optional[
                    tp.Callable[[tp.List[ConditionAttributes]], tp.List[ConditionAttributes]]] = None,
                 text_or_audio: str = 'audio') -> torch.Tensor:
        """Generate tokens sampling from the model given a prompt or unconditionally. Generation can
        be perform in a greedy fashion or using sampling with top K and top P strategies.

        Args:
            prompt (torch.Tensor, optional): Prompt tokens of shape [B, Kt + Ka, T]. When the model supports text,
                `Kt` is 1. The text token is at index 0. `Ka` is the number of audio codebooks.
            conditions_tensors (list of ConditioningAttributes, optional): List of conditions.
            num_samples (int, optional): Number of samples to generate when no prompt and no conditions are given.
            max_gen_len (int): Maximum generation length.
            use_sampling (bool): Whether to use a sampling strategy or not.
            temp (float): Sampling temperature.
            top_k (int): K for "top-k" sampling.
            top_p (float): P for "top-p" sampling.
            strip (int): number of time steps to strip from the prompt to avoid padding artifacts. 1 should be enough.
            cfg_coef (float, optional): Classifier-free guidance coefficient.
            two_step_cfg (bool, optional): Whether to perform classifier-free guidance with two steps generation.
            check (bool): Whether to apply further checks on generated sequence.
            min_start_offset (int or None): if provided, always replays the generation at least from that offset,
                even if the prompt is longer. This is to ensure the same number of steps on different GPUs with varying
                prompt, to remove any chance of deadlock with FSDP in the case where it might OOM (and ask again
                for the other shards).
            callback (Callback, optional): Callback function to report generation progress.
            postprocess_conditions (Callable, optional): This will get called with the condition tensors
                (potentially with CFG stacking) and is currently the only way to mess up with the conditioning.
            get_null_conditions (Callable, optional): override how to obtain the null conditions when CFG is used.
                Takes as input a list of ConditionAttributes, and returns the same.
            text_or_audio (str): controls whether to generate only text, only audio, or both.
         Returns:
            torch.Tensor: Generated tokens, with shape `[B, Kt + Ka, T]`. Note that even if only one modality
                is generated, the output always contains `Kt + Ka` tokens.

        """
        assert not self.training, "generation shouldn't be used in training mode."
        first_param = next(iter(self.parameters()))
        device = first_param.device

        # Checking all input shapes are consistent.
        possible_num_samples = []
        if num_samples is not None:
            possible_num_samples.append(num_samples)
        elif prompt is not None:
            possible_num_samples.append(prompt.shape[0])
        elif conditions:
            possible_num_samples.append(len(conditions))
        else:
            possible_num_samples.append(1)
        assert [x == possible_num_samples[0] for x in possible_num_samples], "Inconsistent inputs shapes"
        num_samples = possible_num_samples[0]
        assert isinstance(num_samples, int)

        # below we create set of conditions: one conditional and one unconditional
        # to do that we merge the regular condition together with the null condition
        # we then do 1 forward pass instead of 2.
        # the reason for that is two-fold:
        # 1. it is about x2 faster than doing 2 forward passes
        # 2. avoid the streaming API treating the 2 passes as part of different time steps
        # We also support doing two different passes, in particular to ensure that
        # the padding structure is exactly the same between train and test.
        # With a batch size of 1, this can be slower though.
        cfg_conditions: CFGConditions
        two_step_cfg = self.two_step_cfg if two_step_cfg is None else two_step_cfg
        cfg_coef = self.cfg_coef if cfg_coef is None else cfg_coef
        if conditions:
            if cfg_coef == 1.:
                prepared = self.condition_provider.prepare(conditions)

                cfg_conditions = self.condition_provider(prepared)
            else:
                if get_null_conditions is None:
                    null_conditions = ClassifierFreeGuidanceDropout(p=1.0)(conditions)
                else:
                    null_conditions = get_null_conditions(conditions)
                if two_step_cfg:
                    cfg_conditions = (
                        self.condition_provider(self.condition_provider.prepare(conditions)),
                        self.condition_provider(self.condition_provider.prepare(null_conditions)),
                    )
                else:
                    conditions = conditions + null_conditions
                    prepared = self.condition_provider.prepare(conditions)
                    cfg_conditions = self.condition_provider(prepared)
        else:
            cfg_conditions = {}

        if postprocess_conditions is not None:
            cfg_conditions = postprocess_conditions(cfg_conditions)

        initial = self._get_initial_token(text_or_audio).expand(num_samples, -1, -1)
        max_delay = max(self.delays)  # with delays, we need to generate a few more time steps.
        ungenerated = self.ungenerated_token_id  # special value to indicate tokens to generate
        zero = torch.full([1], self.zero_token_id, device=device, dtype=torch.long)
        gen_sequence = torch.full((num_samples, self.num_codebooks, max_gen_len + max_delay + 1),
                                  ungenerated, device=device, dtype=torch.long)
        # special token for the beginning of the sequence.
        gen_sequence[:, :, :1] = initial
        start_offset = 0

        if prompt is not None:
            assert prompt.shape[-1] > strip, f"Prompt should be longer than strip={strip} time steps."
            if strip:
                prompt = prompt[..., :-strip]
            assert start_offset < max_gen_len
            PT = prompt.shape[-1]
            for cb in range(self.num_codebooks):
                delay = self.delays[cb]
                gen_sequence[:, cb, :delay + 1] = initial[:, cb]
                gen_sequence[:, cb, delay + 1: delay + 1 + PT] = prompt[:, cb, :]
            # We look for the first time step that is ungenerated, as we allow for partial teacher
            # forcing, for instance by providing the text and generating the audio or the opposite.
            ungenerated_steps = (gen_sequence == ungenerated).nonzero()[:, 2]
            if not ungenerated_steps.numel():
                raise RuntimeError("Nothing to generate.")
            # start offset will be one step before the first value to generate.
            # The `-1` offset is because time step T is generated as the output of
            # timestep T - 1.
            start_offset = int(ungenerated_steps.amin()) - 1
            if min_start_offset is not None:
                start_offset = min(min_start_offset, start_offset)
            assert start_offset >= 0
            logger.debug("Start offset is %d", start_offset)

        audio_offset = self.audio_offset
        context = self.text_context if text_or_audio == 'text' else self.context
        set_attention_context(self.transformer, context)
        with self.streaming(), self.autocast:
            unconditional_state = dict(self.get_streaming_state())
            for offset in range(start_offset, max_gen_len + max_delay):
                # `offset` measures position in the output tensor with no delays.
                # In particular, there is a shift of 1 with the `gen_sequence` that includes
                # the initial empty token.
                logger.debug("Offset %d / %d", offset, max_gen_len + max_delay)
                # get current sequence (note that the streaming API is providing the caching over previous offsets)

                if offset == start_offset:
                    input_ = gen_sequence[:, :, :offset + 1]
                else:
                    input_ = gen_sequence[:, :, offset:offset + 1]

                if check:
                    # Check that we are not feeding in any value that is not generated yet.
                    assert not (input_ == ungenerated).any(), (offset, input_)
                    assert (input_[:, self.audio_offset:] <= self.card).all(), input_
                    if self.has_text:
                        assert (input_[:, :1] <= self.text_card).all()

                next_token, _ = self._sample_next_token(
                    input_, cfg_conditions, unconditional_state,
                    use_sampling, temp, top_k, top_p,
                    cfg_coef=cfg_coef, two_step_cfg=two_step_cfg, text_or_audio=text_or_audio)
                assert next_token.shape[-1] == 1
                next_token = next_token[:, :, 0]   # shape is [B, K]
                this_gen_step = gen_sequence[:, :, offset + 1]

                if self.depformer is None:
                    if text_or_audio == 'audio' and self.has_text:
                        # We want audio only, but the model also supports text, we insert a
                        # padding token for the text.
                        next_token = torch.cat([zero.expand(len(next_token), 1), next_token], dim=1)
                    elif text_or_audio == 'text' and self.num_audio_codebooks > 0:
                        # We want text only, but the model also supports audio, we insert
                        # as many padding tokens as the number of audio codebooks.
                        next_token = torch.cat([
                            next_token, zero.expand(len(next_token), self.num_audio_codebooks)], dim=1)
                    next_token = torch.where(this_gen_step == ungenerated, next_token, this_gen_step)

                else:
                    # Depformer gives us tokens one by one instead of K at once.
                    assert next_token.shape[1] == 1, next_token.shape[1]
                    next_token = next_token[:, 0]  # Now shape is B.
                    depformer_tokens: tp.List[torch.Tensor] = []
                    for cb_index in range(self.num_codebooks):
                        if cb_index == 0 and self.has_text and text_or_audio == 'audio':
                            # We are not generating text, we can fill the text with zero tokens.
                            depformer_tokens.insert(0, zero.expand(len(next_token)))
                            continue
                        if cb_index > 0 and text_or_audio == 'text':
                            # We are not generating audio, filling up with zero tokens.
                            depformer_tokens.append(zero.expand(len(next_token)))
                            continue
                        if cb_index == 0:
                            # No need to generate, `next_token` is actually the next text token.
                            # We just need to only keep the new token if the value wasn't provided
                            # in the prompt.
                            next_token = torch.where(this_gen_step[:, 0] == ungenerated,
                                                     next_token, this_gen_step[:, 0])
                        elif cb_index == 1 and self.has_text and text_or_audio == 'audio':
                            # No need to generate, `next_token` is actually the next audio token.
                            next_token = torch.where(this_gen_step[:, 1] == ungenerated,
                                                     next_token, this_gen_step[:, 1])
                        else:
                            input_ = next_token[:, None, None]
                            if check:
                                # Check that we are not feeding in any value that is not generated yet.
                                assert not (input_ == ungenerated).any()
                                if self.has_text and cb_index == 1:
                                    assert (input_ <= self.text_card).all()
                                else:
                                    assert (input_ <= self.card).all()
                            next_token, _ = self._sample_next_token(
                                input_, cfg_conditions, unconditional_state,
                                use_sampling, temp, top_k, top_p,
                                cfg_coef=cfg_coef, two_step_cfg=two_step_cfg, text_or_audio=text_or_audio)
                            assert next_token.shape[-1] == 1
                            next_token = next_token[:, 0, 0]   # shape is [B, K]
                            next_token = torch.where(this_gen_step[:, cb_index] == ungenerated,
                                                     next_token, this_gen_step[:, cb_index])

                        original_offset = offset - self.delays[cb_index]
                        if original_offset < 0:
                            # We are not currently generating this codebook, we replace with a special token.
                            next_token[:] = initial[:, cb_index, 0]
                        depformer_tokens.append(next_token)

                    assert len(depformer_tokens) == self.num_codebooks, (len(depformer_tokens), self.num_codebooks)
                    next_token = torch.stack(depformer_tokens, dim=1)
                    assert next_token.shape == (num_samples, self.num_codebooks), next_token.shape

                # ensure we don't overwrite prompt tokens, we only write over ungenerated tokens
                gen_sequence[..., offset + 1] = next_token
                if callback is not None:
                    callback(1 + offset - start_offset, max_gen_len + max_delay - start_offset)
        unconditional_state.clear()

        output, mask = _undelay_sequence(self.delays, gen_sequence[:, :, 1:], fill_value=ungenerated)
        assert mask[:, :, :max_gen_len].all()
        output = output[:, :, :max_gen_len]
        tgt_shape = (num_samples, self.num_codebooks, max_gen_len)
        assert output.shape == tgt_shape, (output.shape, tgt_shape)
        # ensure sequence has been entirely filled
        assert not (output == ungenerated).any()
        # ensure the returned codes are all valid
        if text_or_audio == 'audio':
            assert (output[:, audio_offset:] < self.card).all()
        if text_or_audio == 'text':
            assert (output[:, :1] <= self.text_card).all()
        return output
