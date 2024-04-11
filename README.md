Kconfiglib is a Python library for processing [Kconfig](https://www.kernel.org/doc/html/latest/kbuild/kconfig-language.html) files. It can be used to query information about the options, choices, menus, and comments in Kconfig files, and to generate configuration files.

> [!NOTE]
> This repository is a modified version of the original [Kconfiglib](https://github.com/ulfalizer/Kconfiglib)


## Installation

Clone the repository and install the package using pip:

```bash
git clone https://github.com/0x4248/Kconfiglib
cd Kconfiglib
python -m pip install build
python -m build
python -m pip install dist/*.whl
```