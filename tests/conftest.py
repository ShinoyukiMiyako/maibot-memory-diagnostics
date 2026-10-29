from pathlib import Path

import sys
import types

# 宿主按 spec_from_file_location(..., submodule_search_locations=[插件目录]) 装载插件，插件目录相当于一个包，
# 同目录模块只能相对导入。测试注册一个以插件目录为 __path__ 的空包 memdiag，按同样的方式导入各模块。
PLUGIN_DIR = Path(__file__).resolve().parents[1]
if "memdiag" not in sys.modules:
    package = types.ModuleType("memdiag")
    package.__path__ = [str(PLUGIN_DIR)]
    sys.modules["memdiag"] = package
