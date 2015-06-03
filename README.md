# Kconfiglib #

A Python library for doing stuff with Kconfig-based configuration systems. Can
extract information, query and set symbol values, and read and write
<i>.config</i> files. Highly compatible with the <i>scripts/kconfig/\*conf</i>
utilities in the kernel, usually invoked via make targets such as
<i>menuconfig</i> and <i>defconfig</i>.

One feature is missing: Kconfiglib assumes the modules symbol is `MODULES`, and
will warn if `option modules` is set on some other symbol. Let me know if this
is a problem for you, as adding support shouldn't be that hard. I haven't seen
modules used outside the kernel, where the name is unlikely to change.

## Installation ##

### Installation instructions for the Linux kernel ###

Run the following commands in the kernel root:

    $ git clone git://github.com/ulfalizer/Kconfiglib.git  
    $ git am Kconfiglib/makefile.patch

<i>(Note: The directory name Kconfiglib/ is significant.)</i>

In addition to creating a handy interface, the make targets created by the patch
(`scriptconfig` and `iscripconfig`) are needed to pick up environment variables
set in the kernel makefiles and later referenced in the Kconfig files (<i>ARCH</i>,
<i>SRCARCH</i>, and <i>KERNELVERSION</i> as of Linux v4.0-rc3). The documentation
explains how the make targets are used. The compatibility tests in the test suite
also needs them.

Please tell me if the patch does not apply. It should be trivial to apply
manually.

### Installation instructions for other projects ###

The entire library is contained in [kconfiglib.py](kconfiglib.py). Drop it
somewhere and read the documentation. Make sure Kconfiglib sees environment
variables referenced in the configuration.

## Documentation ##

The (extensive) documentation is generated by running

    $ pydoc kconfiglib

in the <i>Kconfiglib/</i> directory. For HTML output,
use

    $ pydoc -w kconfiglib
    
You could also browse the docstrings directly in [kconfiglib.py](kconfiglib.py).

Please tell me if something is unclear to you or can be explained better. The Kconfig
language has some dark corners.

## Examples ##

 * The [examples/](examples/) directory contains simple example scripts. See the documentation for how to run them.

 * [gen-manual-lists.py](http://git.buildroot.net/buildroot/tree/support/scripts/gen-manual-lists.py) from [Buildroot](http://buildroot.uclibc.org/) generates listings for the [appendix of the manual](http://buildroot.uclibc.org/downloads/manual/manual.html#_appendix). Due to an oversight, there were no APIs for fetching prompts from symbols and choices when it was written. Those have been added.

 * [genboardscfg.py](http://git.denx.de/?p=u-boot.git;a=blob;f=tools/genboardscfg.py;hb=HEAD) from [Das U-Boot](http://www.denx.de/wiki/U-Boot) generates some sort of legacy board database by pulling information from a newly added Kconfig-based configuration system (as far as I understand it :).

 * [kconfig-diff.py](https://gist.github.com/dubiousjim/5638961) -- a script by [dubiousjim](https://github.com/dubiousjim) that compares kernel configurations.

 * Originally, Kconfiglib was used in chapter 4 of my [master's thesis](http://liu.diva-portal.org/smash/get/diva2:473038/FULLTEXT01.pdf) to automatically generate a "minimal" kernel for a given system. Parts of it bother me a bit now, but that's how it goes with old work.
 
## Test suite ##

The test suite is run with

    $ python Kconfiglib/testsuite.py

It comprises a set of selftests and a set of compatibility tests that compare
configurations generated by Kconfiglib with configurations generated by
<i>scripts/kconfig/conf</i> for a number of cases. You might want to use the
"speedy" option; see [testsuite.py](testsuite.py).

## Misc. notes ##

 * Python 2 is used at the moment.

 * Kconfiglib works well with [PyPy](http://pypy.org). It might give a nice
speedup over CPython when batch processing a large number of configurations,
as well as when running the test suite.

 * Please tell me if you miss some API instead of digging into internals. The
internal data structures and APIs, and dependency stuff in particular, are
unlikely to be exactly what you want as a user (hence why they're internal :).
Patches are welcome too of course. ;)

 * At least two things make it a bit awkward to replicate a 'menuconfig'-like
   interface in Kconfiglib at the moment. APIs could be added if needed.

   * There are no good APIs for figuring out what other symbols change in value
     when the value of some symbol is changed, to allow for "live" updates
     in the configuration interface. The simplest workaround is to refetch the
     value of each currently visible symbol every time a symbol value is changed.

   * 'menuconfig' sometimes creates menus implicitly by looking at dependencies.
     For example, a list of symbols where all symbols depend on the first symbol
     might create such a menu rooted at the first symbol. Recreating such "cosmetic"
     menus might be awkward.

 * [fpemud](https://github.com/fpemud) has put together [Python
bindings](https://github.com/fpemud/pylkc) to internal functions in the C
implementation. This is an alternative to Kconfiglib's all-Python approach.

 * The test suite failures (should be the only ones) for the following Blackfin
defconfigs on e.g. Linux 3.7.0-rc8 are due to
[a bug in the C implementation](https://lkml.org/lkml/2012/12/5/458):

   * arch/blackfin/configs/CM-BF537U\_defconfig  
   * arch/blackfin/configs/BF548-EZKIT\_defconfig  
   * arch/blackfin/configs/BF527-EZKIT\_defconfig  
   * arch/blackfin/configs/BF527-EZKIT-V2\_defconfig  
   * arch/blackfin/configs/TCM-BF537\_defconfig

## Thanks ##

Thanks to [Philip Craig](https://github.com/philipc) for adding
support for the `allnoconfig_y` option and fixing an obscure issue
with `comment`s inside `choice`s (that didn't affect correctness but
made outputs differ). `allnoconfig_y` is used to force certain symbols
to `y` during `make allnoconfig` to improve coverage.

## License (ISC) ##

Copyright (c) 2011-2015, Ulf Magnusson <ulfalizer@gmail.com>

Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
