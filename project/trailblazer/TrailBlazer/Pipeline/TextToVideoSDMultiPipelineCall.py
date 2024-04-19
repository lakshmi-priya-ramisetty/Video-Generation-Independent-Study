import tqdm
import inspect
from typing import Any, Callable, Dict, List, Optional, Union

import numpy as np
import torch
from transformers import CLIPTextModel, CLIPTokenizer
from dataclasses import dataclass

from diffusers.loaders import LoraLoaderMixin, TextualInversionLoaderMixin
from diffusers.models import AutoencoderKL, UNet3DConditionModel
from diffusers.models.lora import adjust_lora_scale_text_encoder
from diffusers.schedulers import KarrasDiffusionSchedulers
from diffusers.utils import (
    deprecate,
    logging,
    replace_example_docstring,
    BaseOutput,
)
from diffusers.pipelines.text_to_video_synthesis.pipeline_text_to_video_synth import (
    tensor2vid,
)

from ..Misc import Logger as log
from ..Misc import Const
from ..Misc.BBox import BoundingBox
from .Utils import initiailization, keyframed_bbox, keyframed_prompt_embeds, use_dd, use_dd_temporal

@dataclass
class TextToVideoSDPipelineOutput(BaseOutput):
    """
    Output class for text-to-video pipelines.

    Args:
        frames (`List[np.ndarray]` or `torch.FloatTensor`)
            List of denoised frames (essentially images) as NumPy arrays of shape `(height, width, num_channels)` or as
            a `torch` tensor. The length of the list denotes the video length (the number of frames).
    """

    frames: Union[List[np.ndarray], torch.FloatTensor]
    latents: Union[List[np.ndarray], torch.FloatTensor]
    bbox_per_frame: torch.tensor


@torch.no_grad()
def text_to_video_sd_multi_pipeline_call(
    self,
    bundle=None,
    # prompt: Union[str, List[str]] = None,
    height: Optional[int] = None,
    width: Optional[int] = None,
    # num_frames: int = 16,
    num_inference_steps: int = 50,
    # num_dd_steps: int = 0,
    guidance_scale: float = 9.0,
    negative_prompt: Optional[Union[str, List[str]]] = None,
    eta: float = 0.0,
    generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
    latents: Optional[torch.FloatTensor] = None,
    prompt_embeds: Optional[torch.FloatTensor] = None,
    negative_prompt_embeds: Optional[torch.FloatTensor] = None,
    output_type: Optional[str] = "np",
    return_dict: bool = True,
    callback: Optional[Callable[[int, int, torch.FloatTensor], None]] = None,
    callback_steps: int = 1,
    cross_attention_kwargs: Optional[Dict[str, Any]] = None,
    progress = None,
):
    r"""
    The call function to the pipeline for generation.

    Args:
        prompt (`str` or `List[str]`, *optional*):
            The prompt or prompts to guide image generation. If not defined, you need to pass `prompt_embeds`.
        height (`int`, *optional*, defaults to `self.unet.config.sample_size * self.vae_scale_factor`):
            The height in pixels of the generated video.
        width (`int`, *optional*, defaults to `self.unet.config.sample_size * self.vae_scale_factor`):
            The width in pixels of the generated video.
        num_frames (`int`, *optional*, defaults to 16):
            The number of video frames that are generated. Defaults to 16 frames which at 8 frames per seconds
            amounts to 2 seconds of video.
        num_inference_steps (`int`, *optional*, defaults to 50):
            The number of denoising steps. More denoising steps usually lead to a higher quality videos at the
            expense of slower inference.
        guidance_scale (`float`, *optional*, defaults to 7.5):
            A higher guidance scale value encourages the model to generate images closely linked to the text
            `prompt` at the expense of lower image quality. Guidance scale is enabled when `guidance_scale > 1`.
        negative_prompt (`str` or `List[str]`, *optional*):
            The prompt or prompts to guide what to not include in image generation. If not defined, you need to
            pass `negative_prompt_embeds` instead. Ignored when not using guidance (`guidance_scale < 1`).
        num_images_per_prompt (`int`, *optional*, defaults to 1):
            The number of images to generate per prompt.
        eta (`float`, *optional*, defaults to 0.0):
            Corresponds to parameter eta (η) from the [DDIM](https://arxiv.org/abs/2010.02502) paper. Only applies
            to the [`~schedulers.DDIMScheduler`], and is ignored in other schedulers.
        generator (`torch.Generator` or `List[torch.Generator]`, *optional*):
            A [`torch.Generator`](https://pytorch.org/docs/stable/generated/torch.Generator.html) to make
            generation deterministic.
        latents (`torch.FloatTensor`, *optional*):
            Pre-generated noisy latents sampled from a Gaussian distribution, to be used as inputs for video
            generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
            tensor is generated by sampling using the supplied random `generator`. Latents should be of shape
            `(batch_size, num_channel, num_frames, height, width)`.
        prompt_embeds (`torch.FloatTensor`, *optional*):
            Pre-generated text embeddings. Can be used to easily tweak text inputs (prompt weighting). If not
            provided, text embeddings are generated from the `prompt` input argument.
        negative_prompt_embeds (`torch.FloatTensor`, *optional*):
            Pre-generated negative text embeddings. Can be used to easily tweak text inputs (prompt weighting). If
            not provided, `negative_prompt_embeds` are generated from the `negative_prompt` input argument.
        output_type (`str`, *optional*, defaults to `"np"`):
            The output format of the generated video. Choose between `torch.FloatTensor` or `np.array`.
        return_dict (`bool`, *optional*, defaults to `True`):
            Whether or not to return a [`~pipelines.text_to_video_synthesis.TextToVideoSDPipelineOutput`] instead
            of a plain tuple.
        callback (`Callable`, *optional*):
            A function that calls every `callback_steps` steps during inference. The function is called with the
            following arguments: `callback(step: int, timestep: int, latents: torch.FloatTensor)`.
        callback_steps (`int`, *optional*, defaults to 1):
            The frequency at which the `callback` function is called. If not specified, the callback is called at
            every step.
        cross_attention_kwargs (`dict`, *optional*):
            A kwargs dictionary that if specified is passed along to the [`AttentionProcessor`] as defined in
            [`self.processor`](https://github.com/huggingface/diffusers/blob/main/src/diffusers/models/attention_processor.py).

    Examples:

    Returns:
        [`~pipelines.text_to_video_synthesis.TextToVideoSDPipelineOutput`] or `tuple`:
            If `return_dict` is `True`, [`~pipelines.text_to_video_synthesis.TextToVideoSDPipelineOutput`] is
            returned, otherwise a `tuple` is returned where the first element is a list with the generated frames.
    """

    prompt = bundle["multisubs"]["prompt"]
    num_integration_steps = bundle["multisubs"]["num_integration_steps"]
    peek = torch.load(bundle["multisubs"]["subjects"][0])
    num_frames = len(peek["bbox"])

    #prompt += Const.POSITIVE_PROMPT
    subject_latents = []
    subject_bboxes = []
    for path in bundle["multisubs"]["subjects"]:
        print(path)
        data = torch.load(path)
        latent = data["latents"]
        bundle_ = data["bundle"]
        subject_latents.append(latent)
        bboxes = []
        for bbox_ratio in data["bbox"]:
            dim_x = latent.shape[-1]
            dim_y = latent.shape[-1]
            bbox = BoundingBox(dim_x = dim_x, dim_y = dim_y, box_ratios=bbox_ratio)
            bboxes.append(bbox)
        subject_bboxes.append(bboxes)

    #initiailization(unet=self.unet, bundle=bundle, bbox_per_frame=bbox_per_frame)

    from pprint import pprint


    # 0. Default height and width to unet
    height = height or self.unet.config.sample_size * self.vae_scale_factor
    width = width or self.unet.config.sample_size * self.vae_scale_factor

    num_images_per_prompt = 1
    negative_prompt = Const.NEGATIVE_PROMPT

    # # 2. Define call parameters
    if prompt is not None and isinstance(prompt, str):
        batch_size = 1
    elif prompt is not None and isinstance(prompt, list):
        batch_size = len(prompt)
    else:
        batch_size = prompt_embeds.shape[0]

    device = self._execution_device
    # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
    # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
    # corresponds to doing no classifier free guidance.
    do_classifier_free_guidance = guidance_scale > 1.0

    # 3. Encode input prompt
    text_encoder_lora_scale = (
        cross_attention_kwargs.get("scale", None)
        if cross_attention_kwargs is not None
        else None
    )

    # prompt_embeds, negative_prompt_embeds = self.encode_prompt(
    #     prompt,
    #     device,
    #     num_images_per_prompt,
    #     do_classifier_free_guidance,
    #     negative_prompt,
    #     prompt_embeds=prompt_embeds,
    #     negative_prompt_embeds=negative_prompt_embeds,
    #     lora_scale=text_encoder_lora_scale,
    # )

    bundle["keyframe"] = bundle_["keyframe"]
    for key in bundle["keyframe"]:
        key["prompt"] = bundle["multisubs"]["prompt"]
    log.info("Experiment parameters:")
    print("==========================================")
    pprint(bundle)
    print("==========================================")
    prompt_embeds, negative_prompt_embeds = keyframed_prompt_embeds(
        bundle, self.encode_prompt, device
    )

    # For classifier free guidance, we need to do two forward passes.
    # Here we concatenate the unconditional and text embeddings into a single batch
    # to avoid doing two forward passes
    if do_classifier_free_guidance:
        prompt_embeds = torch.cat([negative_prompt_embeds, prompt_embeds])

    # 4. Prepare timesteps
    self.scheduler.set_timesteps(num_inference_steps, device=device)
    timesteps = self.scheduler.timesteps

    # 5. Prepare latent variables
    num_channels_latents = self.unet.config.in_channels
    latents = self.prepare_latents(
        batch_size * num_images_per_prompt,
        num_channels_latents,
        num_frames,
        height,
        width,
        prompt_embeds.dtype,
        device,
        generator,
        latents,
    )

    # 6. Prepare extra step kwargs. TODO: Logic should ideally just be moved out of the pipeline
    extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)

    # 7. Denoising loop
    num_warmup_steps = len(timesteps) - num_inference_steps * self.scheduler.order

    latents_at_steps = []
    # NOTE: Since this is the simply latent integration. We don't need DD here
    use_dd(self.unet, False)
    use_dd_temporal(self.unet, False)
    with self.progress_bar(total=num_inference_steps) as progress_bar:

        if type(progress)!=type(None):
            timesteps = progress.tqdm(timesteps, desc="Processing")

        i = 0
        for i, t in enumerate(timesteps):

            # expand the latents if we are doing classifier free guidance
            latent_model_input = (
                torch.cat([latents] * 2) if do_classifier_free_guidance else latents
            )
            latent_model_input = self.scheduler.scale_model_input(latent_model_input, t)

            noise_pred = self.unet(
                latent_model_input,
                t,
                encoder_hidden_states=prompt_embeds,
                cross_attention_kwargs=cross_attention_kwargs,
                return_dict=False,
            )[0]


            # perform guidance
            if do_classifier_free_guidance:
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                noise_pred = noise_pred_uncond + guidance_scale * (
                    noise_pred_text - noise_pred_uncond
                )

            # reshape latents
            bsz, channel, frames, width, height = latents.shape
            latents = latents.permute(0, 2, 1, 3, 4).reshape(
                bsz * frames, channel, width, height
            )
            noise_pred = noise_pred.permute(0, 2, 1, 3, 4).reshape(
                bsz * frames, channel, width, height
            )

            # compute the previous noisy sample x_t -> x_t-1
            latents = self.scheduler.step(
                noise_pred, t, latents, **extra_step_kwargs
            ).prev_sample

            # reshape latents back
            latents = (
                latents[None, :]
                .reshape(bsz, frames, channel, width, height)
                .permute(0, 2, 1, 3, 4)
            )
            latents_at_steps.append(latents)

            if i < num_integration_steps:
                margin = -.1
                #a = 0.01
                a = 1.0 - (num_integration_steps - i) * 1.0 / num_integration_steps
                combined_latent = latents.clone()
                combined_latents = []
                for j, subject_latent in enumerate(subject_latents):
                    for f, bbox in enumerate(subject_bboxes[j]):
                        top = int(bbox.top + margin)
                        bottom = int(bbox.bottom - margin)
                        left = int(bbox.left + margin)
                        right = int(bbox.right - margin)
                        combined_latent[..., f, top:bottom, left:right] = (
                            a * latents[..., f, top:bottom, left:right]
                            + (1.0 - a)
                            * subject_latent[i][..., f, top:bottom, left:right]
                        )
                    combined_latents.append(combined_latent)
                latents = torch.cat(combined_latents).mean(dim=0, keepdim=True)

            # call the callback, if provided
            if i == len(timesteps) - 1 or (
                (i + 1) > num_warmup_steps and (i + 1) % self.scheduler.order == 0
            ):
                progress_bar.update()
                if callback is not None and i % callback_steps == 0:
                    callback(i, t, latents)
            i += 1

    if output_type == "latent":
        return TextToVideoSDPipelineOutput(frames=latents)

    video_tensor = self.decode_latents(latents)

    if output_type == "pt":
        video = video_tensor
    else:
        video = tensor2vid(video_tensor)

    # Offload all models
    self.maybe_free_model_hooks()

    if not return_dict:
        return (video,)

    latents_at_steps = torch.cat(latents_at_steps)
    return TextToVideoSDPipelineOutput(frames=video, latents=latents_at_steps, bbox_per_frame=None)
