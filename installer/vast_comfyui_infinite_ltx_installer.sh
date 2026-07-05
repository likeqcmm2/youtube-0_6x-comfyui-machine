#!/usr/bin/env bash
set -Eeuo pipefail

# Vast.ai ComfyUI RTX 5090 installer for:
# - Wan InfiniteTalk + SageAttention3 workflow
# - LTX I2V FFLF Custom Audio workflow
#
# Run on the Vast server as root:
#   bash vast_comfyui_infinite_ltx_installer.sh
#
# Upload workflows before or after the installer. The installer always restarts
# api-wrapper and patches converted payloads if workflows are present.

COMFY_DIR="${COMFY_DIR:-/workspace/ComfyUI}"
VENV_DIR="${VENV_DIR:-/venv/main}"
CUDA_HOME_DEFAULT="${CUDA_HOME:-/usr/local/cuda-12.9}"
HF_HOME="${HF_HOME:-/workspace/.hf_home}"
HF_DOWNLOAD_ROOT="${HF_DOWNLOAD_ROOT:-/workspace/hf-downloads}"
MAX_JOBS="${MAX_JOBS:-8}"
NVCC_APPEND_FLAGS="${NVCC_APPEND_FLAGS:---threads 4}"
CONVERT_WORKFLOWS=1

for arg in "$@"; do
  case "$arg" in
    --convert-workflows) CONVERT_WORKFLOWS=1 ;;
    --help|-h)
      sed -n '1,35p' "$0"
      exit 0
      ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\n== %s ==\n' "$*"; }
have_file() { [ -s "$1" ]; }

require_root() {
  if [ "$(id -u)" != "0" ]; then
    echo "Run this installer as root on the Vast ComfyUI instance." >&2
    exit 1
  fi
}

activate_venv() {
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
}

install_system_deps() {
  log "Installing system dependencies"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    git wget ca-certificates aria2 \
    libcusparse-dev-12-9 libcublas-dev-12-9 libcusolver-dev-12-9 \
    ninja-build
}

install_python_deps() {
  log "Installing Python download/build dependencies"
  activate_venv
  pip install -U "huggingface_hub" hf-xet ninja packaging
}

install_custom_node() {
  local url="$1"
  local dir="$2"
  cd "$COMFY_DIR"
  if [ ! -d "custom_nodes/$dir" ]; then
    log "Cloning custom node: $dir"
    git clone --depth 1 "$url" "custom_nodes/$dir"
  else
    log "Updating custom node: $dir"
    git -C "custom_nodes/$dir" pull --ff-only || true
  fi
  if [ -f "custom_nodes/$dir/requirements.txt" ]; then
    activate_venv
    pip install -r "custom_nodes/$dir/requirements.txt"
  fi
}

install_custom_nodes() {
  log "Installing custom nodes"
  install_custom_node https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite ComfyUI-VideoHelperSuite
  install_custom_node https://github.com/WhatDreamsCost/WhatDreamsCost-ComfyUI.git WhatDreamsCost-ComfyUI
  install_custom_node https://github.com/kijai/ComfyUI-KJNodes.git ComfyUI-KJNodes
  install_custom_node https://github.com/yolain/ComfyUI-Easy-Use.git ComfyUI-Easy-Use
  install_custom_node https://github.com/evanspearman/ComfyMath.git ComfyMath
}

hf_file() {
  local repo="$1"
  local repo_file="$2"
  local dest_dir="$3"
  local dest_file="$4"
  local expected_size="${5:-0}"
  local dst="$COMFY_DIR/models/$dest_dir/$dest_file"
  local local_dir="$HF_DOWNLOAD_ROOT/${repo//\//__}"

  mkdir -p "$COMFY_DIR/models/$dest_dir" "$local_dir"
  if have_file "$dst"; then
    local actual_size
    actual_size="$(stat -c %s "$dst")"
    if [ "$expected_size" = "0" ] || [ "$actual_size" = "$expected_size" ]; then
      echo "HAVE $dst $actual_size bytes"
      return 0
    fi
    echo "SIZE MISMATCH $dst has $actual_size bytes, expected $expected_size; moving aside"
    mv -f "$dst" "$dst.partial.$(date +%s)"
  fi

  log "Downloading $repo :: $repo_file"
  activate_venv
  export HF_HOME
  export HF_XET_HIGH_PERFORMANCE=1
  unset HF_HUB_ENABLE_HF_TRANSFER || true
  unset HF_HUB_DISABLE_XET || true

  hf download "$repo" "$repo_file" \
    --local-dir "$local_dir" \
    --max-workers 16

  if [ "$local_dir/$repo_file" != "$dst" ]; then
    mkdir -p "$(dirname "$dst")"
    cp -f "$local_dir/$repo_file" "$dst"
  fi
  local final_size
  final_size="$(stat -c %s "$dst")"
  if [ "$expected_size" != "0" ] && [ "$final_size" != "$expected_size" ]; then
    echo "Downloaded $dst has $final_size bytes, expected $expected_size" >&2
    exit 1
  fi
  echo "DONE $dst $final_size bytes"
}

download_models() {
  log "Downloading InfiniteTalk models"
  hf_file Kijai/WanVideo_comfy_fp8_scaled \
    I2V/Wan2_1-I2V-14B-480p_fp8_e4m3fn_scaled_KJ.safetensors \
    diffusion_models/wan2.1 \
    Wan2_1-I2V-14B-480p_fp8_e4m3fn_scaled_KJ.safetensors \
    16643349018
  hf_file Kijai/WanVideo_comfy \
    Lightx2v/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors \
    loras/wan2.2 \
    lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors \
    738005744
  hf_file Comfy-Org/Wan_2.1_ComfyUI_repackaged \
    split_files/model_patches/wan2.1_infiniteTalk_single_fp16.safetensors \
    model_patches \
    wan2.1_infiniteTalk_single_fp16.safetensors \
    5125258232
  hf_file Kijai/wav2vec2_safetensors \
    wav2vec2-chinese-base_fp16.safetensors \
    audio_encoders \
    wav2vec2-chinese-base_fp16.safetensors \
    190115368
  hf_file Comfy-Org/Wan_2.1_ComfyUI_repackaged \
    split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors \
    text_encoders \
    umt5_xxl_fp8_e4m3fn_scaled.safetensors \
    6735906897
  hf_file Kijai/WanVideo_comfy \
    Wan2_1_VAE_bf16.safetensors \
    vae \
    Wan2_1_VAE_bf16.safetensors \
    253806278

  log "Downloading LTX 2.3 models"
  hf_file Lightricks/LTX-2.3-fp8 \
    ltx-2.3-22b-dev-fp8.safetensors \
    checkpoints \
    ltx-2.3-22b-dev-fp8.safetensors \
    29145431166
  hf_file Comfy-Org/ltx-2 \
    split_files/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors \
    text_encoders \
    gemma_3_12B_it_fp4_mixed.safetensors \
    9447702218
  hf_file Kijai/LTX2.3_comfy \
    vae/LTX23_audio_vae_bf16.safetensors \
    vae \
    LTX23_audio_vae_bf16.safetensors \
    364855188
  hf_file Kijai/LTX2.3_comfy \
    vae/LTX23_video_vae_bf16.safetensors \
    vae \
    LTX23_video_vae_bf16.safetensors \
    1452258578
  hf_file Kijai/LTX2.3_comfy \
    vae/taeltx2_3.safetensors \
    vae \
    taeltx2_3.safetensors \
    23531296
  hf_file Lightricks/LTX-2.3 \
    ltx-2.3-spatial-upscaler-x2-1.1.safetensors \
    latent_upscale_models \
    ltx-2.3-spatial-upscaler-x2-1.1.safetensors \
    995743560
  hf_file Kijai/LTX2.3_comfy \
    loras/ltx-2.3-22b-distilled-lora-dynamic_fro09_avg_rank_105_bf16.safetensors \
    loras/ltx2 \
    ltx-2.3-22b-distilled-lora-dynamic_fro09_avg_rank_105_bf16.safetensors \
    2586318182
}

install_sageattention3() {
  log "Building SageAttention3 for Blackwell"
  activate_venv
  mkdir -p /workspace/src
  cd /workspace/src
  if [ ! -d SageAttention ]; then
    git clone https://github.com/thu-ml/SageAttention.git
  else
    git -C SageAttention pull --ff-only || true
  fi

  cd /workspace/src/SageAttention/sageattention3_blackwell
  if python - <<'PY'
import sys
try:
    import torch
    from sageattn3 import sageattn3_blackwell
    q=torch.randn(1,8,128,64,device="cuda",dtype=torch.float16)
    out=sageattn3_blackwell(q,q,q,is_causal=False)
    torch.cuda.synchronize()
    print("SageAttention3 already works", out.shape)
except Exception as e:
    print("SageAttention3 needs build:", repr(e))
    sys.exit(1)
PY
  then
    return 0
  fi

  rm -rf build
  export CUDA_HOME="$CUDA_HOME_DEFAULT"
  export PATH="$CUDA_HOME_DEFAULT/bin:$PATH"
  export LD_LIBRARY_PATH="$CUDA_HOME_DEFAULT/targets/x86_64-linux/lib:${LD_LIBRARY_PATH:-}"
  export MAX_JOBS
  export NVCC_APPEND_FLAGS
  python setup.py install

  if python - <<'PY'
from sageattn3 import sageattn3_blackwell
print("SageAttention3 import check passed")
PY
  then
    true
  else
    warn "SageAttention3 import check failed after build; continuing installer anyway"
  fi
}

enable_sageattention3_in_comfy() {
  log "Enabling SageAttention3 in ComfyUI"
  python3 - <<'PY'
from pathlib import Path
p=Path("/etc/environment")
s=p.read_text() if p.exists() else ""
if "COMFYUI_ARGS=" not in s:
    s += '\nCOMFYUI_ARGS="--disable-auto-launch --disable-xformers --use-sage-attention --port 18188 --enable-cors-header"\n'
elif "--use-sage-attention" not in s:
    lines=[]
    for line in s.splitlines():
        if line.startswith("COMFYUI_ARGS="):
            line=line.rstrip('"') + ' --use-sage-attention"'
        lines.append(line)
    s="\n".join(lines)+"\n"
p.write_text(s)
PY

  cd "$COMFY_DIR"
  python3 - <<'PY'
from pathlib import Path
p=Path("comfy/ldm/modules/attention.py")
s=p.read_text()
bak=Path("comfy/ldm/modules/attention.py.bak_before_sage3")
if not bak.exists():
    bak.write_text(s)
old='''if model_management.sage_attention_enabled():
    logging.info("Using sage attention")
    optimized_attention = attention_sage
elif model_management.flash_attention_enabled():
'''
new='''if model_management.sage_attention_enabled():
    if SAGE_ATTENTION3_IS_AVAILABLE:
        logging.info("Using sage attention 3")
        optimized_attention = attention3_sage
    else:
        logging.info("Using sage attention")
        optimized_attention = attention_sage
elif model_management.flash_attention_enabled():
'''
if "Using sage attention 3" in s:
    print("attention.py already patched")
elif old in s:
    p.write_text(s.replace(old, new))
    print("attention.py patched")
else:
    raise SystemExit("Could not find attention.py patch target")
PY
  python3 -m py_compile comfy/ldm/modules/attention.py
}

patch_uploaded_workflows_and_payloads() {
  log "Patching uploaded workflows and API payloads"
  python3 - <<'PY'
from pathlib import Path
targets = []
for root in [
    Path("/workspace/ComfyUI/user/default/workflows"),
    Path("/opt/comfyui-api-wrapper/payloads"),
]:
    if root.exists():
        targets += list(root.glob("*.json"))

repls = {
    "wan2.1\\\\Wan2_1-I2V-14B-480p_fp8_e4m3fn_scaled_KJ.safetensors": "wan2.1/Wan2_1-I2V-14B-480p_fp8_e4m3fn_scaled_KJ.safetensors",
    "wan2.2\\\\lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors": "wan2.2/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors",
    "ltx2\\\\ltx-2.3-22b-distilled-lora-dynamic_fro09_avg_rank_105_bf16.safetensors": "ltx2/ltx-2.3-22b-distilled-lora-dynamic_fro09_avg_rank_105_bf16.safetensors",
}

for p in targets:
    try:
        s = p.read_text()
    except UnicodeDecodeError:
        continue
    s2 = s
    for old, new in repls.items():
        s2 = s2.replace(old, new)
    if s2 != s:
        p.write_text(s2)
        print("patched", p)
PY
}

restart_services() {
  log "Restarting ComfyUI services"
  supervisorctl restart comfyui || true
  sleep 20
  supervisorctl restart api-wrapper || true
  sleep 10
  patch_uploaded_workflows_and_payloads
}

warn_about_inputs() {
  log "Checking optional workflow input media"
  if [ ! -s "$COMFY_DIR/input/avatar.png" ]; then
    echo "WARNING: InfiniteTalk workflow expects input/avatar.png if you use the guide defaults."
  fi
  if [ ! -s "$COMFY_DIR/input/3.mp3" ]; then
    echo "WARNING: InfiniteTalk workflow expects input/3.mp3 if you use the guide defaults."
  fi
  if [ ! -s "$COMFY_DIR/input/audio.wav" ]; then
    echo "NOTE: LTX default workflow references audio.wav, but default Use Custom Audio is False."
  fi
}

verify_setup() {
  log "Verifying setup"
  python3 - <<'PY'
import json, urllib.request
info=json.load(urllib.request.urlopen("http://127.0.0.1:18188/object_info", timeout=60))
need=[
 "WanInfiniteTalkToVideo","VHS_VideoCombine","VHS_LoadAudioUpload",
 "AudioEncoderLoader","ModelPatchLoader",
 "CM_IntToFloat","LTX2SamplingPreviewOverride","LTXSequencer",
 "LazySwitchKJ","LoadAudioUI","MultiImageLoader","VAELoaderKJ",
 "easy mathInt","LTXAVTextEncoderLoader","LTXVAudioVAEDecode",
 "LTXVAudioVAEEncode","LatentUpscaleModelLoader",
]
missing=[n for n in need if n not in info]
for n in need:
    print(n, "YES" if n in info else "NO")
if missing:
    raise SystemExit("Missing nodes: " + ", ".join(missing))
PY

  tail -220 /var/log/portal/comfyui.log | grep -i -E "Using sage attention 3|Using sage attention|Error running SageAttention3" | tail -40 || true

  find "$COMFY_DIR/models/diffusion_models/wan2.1" \
       "$COMFY_DIR/models/loras/wan2.2" \
       "$COMFY_DIR/models/model_patches" \
       "$COMFY_DIR/models/audio_encoders" \
       "$COMFY_DIR/models/text_encoders" \
       "$COMFY_DIR/models/vae" \
       "$COMFY_DIR/models/checkpoints" \
       "$COMFY_DIR/models/latent_upscale_models" \
       "$COMFY_DIR/models/loras/ltx2" \
       -maxdepth 1 -type f -printf "%p %s bytes\n" | sort
}

main() {
  require_root
  test -d "$COMFY_DIR" || { echo "ComfyUI not found at $COMFY_DIR" >&2; exit 1; }
  test -d "$VENV_DIR" || { echo "Venv not found at $VENV_DIR" >&2; exit 1; }

  install_system_deps
  install_python_deps
  install_custom_nodes
  download_models
  install_sageattention3
  enable_sageattention3_in_comfy
  restart_services
  verify_setup
  warn_about_inputs

  log "Installer complete"
  echo "Upload workflows to: $COMFY_DIR/user/default/workflows/"
  echo "If workflows are uploaded after this run, rerun this installer or run it with --convert-workflows."
}

main "$@"
