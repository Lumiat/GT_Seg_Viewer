#!/usr/bin/env bash
set -euo pipefail
viewer_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${GT_VIEWER_PYTHON:-${EM_VIEWER_PYTHON:-}}" ]]; then
  viewer_python="${GT_VIEWER_PYTHON:-${EM_VIEWER_PYTHON}}"
elif command -v python >/dev/null 2>&1 && python -c 'import numpy,h5py,fastapi,uvicorn' >/dev/null 2>&1; then
  viewer_python="$(command -v python)"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import numpy,h5py,fastapi,uvicorn' >/dev/null 2>&1; then
  viewer_python="$(command -v python3)"
elif [[ -x /gpfs/hulab/lizhenghua/tools/anaconda3/envs/pf-service/bin/python ]]; then
  viewer_python=/gpfs/hulab/lizhenghua/tools/anaconda3/envs/pf-service/bin/python
else
  echo "请先安装依赖：python -m pip install -r $viewer_dir/requirements.txt" >&2
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
# Keep the caller's working directory: --inputs and --cluster resolve there.
exec "$viewer_python" "$viewer_dir/viewer.py" "$@"
