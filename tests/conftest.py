# -*- coding: utf-8 -*-
"""pytest 全局夹具：把仓库根目录加入 sys.path，使 tests 能直接 import 应用模块。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
