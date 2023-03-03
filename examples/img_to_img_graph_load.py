import time
import os
import gc
import shutil
import unittest
import tempfile
from PIL import Image

import numpy as np
import oneflow as flow
import oneflow as torch

from onediff import OneFlowStableDiffusionImg2ImgPipeline as StableDiffusionImg2ImgPipeline

from diffusers import EulerDiscreteScheduler
from diffusers import utils

_model_id = "stabilityai/stable-diffusion-2"
_with_image_save = True


def _cost_cnt(fn):
    def new_fn(*args, **kwargs):
        print("==> function ", fn.__name__, " try to run...")
        flow._oneflow_internal.eager.Sync()
        before_used = flow._oneflow_internal.GetCUDAMemoryUsed()
        print(fn.__name__, " cuda mem before ", before_used, " MB")
        before_host_used = flow._oneflow_internal.GetCPUMemoryUsed()
        print(fn.__name__, " host mem before ", before_host_used, " MB")
        start_time = time.time()
        out = fn(*args, **kwargs)
        flow._oneflow_internal.eager.Sync()
        end_time = time.time()
        print(fn.__name__, " run time ", end_time - start_time, " seconds")
        after_used = flow._oneflow_internal.GetCUDAMemoryUsed()
        print(fn.__name__, " cuda mem after ", after_used, " MB")
        print(fn.__name__, " cuda mem diff ", after_used - before_used, " MB")
        after_host_used = flow._oneflow_internal.GetCPUMemoryUsed()
        print(fn.__name__, " host mem after ", after_host_used, " MB")
        print(fn.__name__, " host mem diff ", after_host_used - before_host_used, " MB")
        print("<== function ", fn.__name__, " finish run.")
        print("")
        return out

    return new_fn


def _reset_session():
    # Close session to avoid the buffer name duplicate error.
    flow.framework.session_context.TryCloseDefaultSession()
    time.sleep(5)
    flow.framework.session_context.NewDefaultSession(flow._oneflow_global_unique_env)


def _test_sd_graph_save_and_load(is_save, graph_save_path, sch_file_path, pipe_file_path):
    if is_save:
        print("\n==> Try to run graph save...")
        _online_mode = False
        _pipe_from_file = False
    else:
        print("\n==> Try to run graph load...")
        _online_mode = True
        _pipe_from_file = True

    total_start_t = time.time()
    start_t = time.time()

    @_cost_cnt
    def get_pipe():
        if _pipe_from_file:
            scheduler = EulerDiscreteScheduler.from_pretrained(sch_file_path, subfolder="scheduler")
            sd_pipe = StableDiffusionImg2ImgPipeline.from_pretrained(
                pipe_file_path, scheduler=scheduler, revision="fp16", torch_dtype=torch.float16
            )
        else:
            scheduler = EulerDiscreteScheduler.from_pretrained(_model_id, subfolder="scheduler")
            sd_pipe = StableDiffusionImg2ImgPipeline.from_pretrained(
                _model_id, scheduler=scheduler, revision="fp16", torch_dtype=torch.float16
            )

        return scheduler, sd_pipe

    sch, pipe = get_pipe()

    @_cost_cnt
    def pipe_to_cuda():
        cu_pipe = pipe.to("cuda")
        return cu_pipe

    pipe = pipe_to_cuda()

    @_cost_cnt
    def config_graph():
        pipe.set_graph_compile_cache_size(9)
        pipe.enable_graph_share_mem()

    config_graph()

    if not _online_mode:
        pipe.enable_save_graph()
    else:
        @_cost_cnt
        def load_graph():
            assert (os.path.exists(graph_save_path) and os.path.isdir(graph_save_path))
            pipe.load_graph(graph_save_path, compile_unet=True, compile_vae=False)

        load_graph()
    end_t = time.time()
    print("sd init time ", end_t - start_t, 's.')

    @_cost_cnt
    def image_to_image(prompt, img, num_images_per_prompt=1, prefix="", with_graph=False):
        cur_generator = torch.Generator("cuda").manual_seed(1024)
        strength = 0.8
        num_inference_steps = 100
        guidance_scale = 10
        eta = 0.

        images = pipe(
            prompt,
            image=img,
            strength=strength,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            compile_unet=with_graph,
            compile_vae=False,
            num_images_per_prompt=num_images_per_prompt,
            eta=eta,
            generator=cur_generator,
            output_type="np",
        ).images

        if _with_image_save:
            for i, image in enumerate(images):
                pipe.numpy_to_pil(image)[0].save(
                    f"{prefix}{prompt}_{i}-with_graph_{str(with_graph)}.png")

        return images

    prompt = "sea,beach,the waves crashed on the sand,blue sky whit white cloud"
    img = Image.new("RGB", (512, 512), "#1f80f0")

    no_g_images = image_to_image(prompt, img, prefix=f"is_save_{str(is_save)}-", with_graph=False)
    with_g_images = image_to_image(prompt, img, prefix=f"is_save_{str(is_save)}-", with_graph=True)
    assert len(no_g_images) == len(with_g_images)
    for img_idx in range(len(no_g_images)):
        print("====> diff ", np.abs(no_g_images[img_idx] - with_g_images[img_idx]).mean())
        # assert np.abs(no_g_images[img_idx] - with_g_images[img_idx]).mean() < 1e-2

    total_end_t = time.time()
    print("st init and run time ", total_end_t - total_start_t, 's.')

    @_cost_cnt
    def save_pipe_sch():
        pipe.save_pretrained(pipe_file_path)
        sch.save_pretrained(sch_file_path)

    @_cost_cnt
    def save_graph():
        assert os.path.exists(graph_save_path) and os.path.isdir(graph_save_path)
        pipe.save_graph(graph_save_path)

    if not _online_mode:
        save_pipe_sch()
        save_graph()


class OneFlowPipeLineGraphSaveLoadTests(unittest.TestCase):
    def tearDown(self):
        # clean up the VRAM after each test
        super().tearDown()
        gc.collect()
        torch.cuda.empty_cache()

    def test_sd_graph_save_and_load(self):
        with tempfile.TemporaryDirectory() as f0:
            with tempfile.TemporaryDirectory() as f1:
                with tempfile.TemporaryDirectory() as f2:
                    _test_sd_graph_save_and_load(True, f0, f1, f2)
                    _reset_session()
                    _test_sd_graph_save_and_load(False, f0, f1, f2)


if __name__ == "__main__":
    unittest.main()
