# This is a test suite for kconfiglib, primarily testing compatibility with the
# C kconfig implementation by comparing outputs. It should be run from the
# top-level kernel directory with
#
# $ PYTHONPATH=scripts/kconfig python scripts/kconfig/kconfigtest.py
#
# (PyPy also works, and runs the defconfig tests roughly 20% faster on my
# machine. Some of the other tests get an even greater speed-up.)
#
# Note that running all of these could take a long time (think many hours on
# fast systems). The tests have been arranged in order of time needed.
#
# All tests should pass. Report regressions to kconfiglib@gmail.com

import kconfiglib
import os
import re
import subprocess
import sys
import textwrap
import time

# Assume that the value of KERNELVERSION does not affect the configuration
# (true as of Linux 2.6.38-rc3). Here we could fetch the correct version
# instead.
os.environ["KERNELVERSION"] = "2"

# Prevent accidental loading of configuration files by removing
# KCONFIG_ALLCONFIG from the environment
os.environ.pop("KCONFIG_ALLCONFIG", None)

# Number of arch/defconfig pairs tested so far
nconfigs = 0

def run_tests():
    run_selftests()
    run_compatibility_tests()

def run_selftests():
    """Runs tests on specific configurations provided by us."""

    print "Running selftests...\n"

    #
    # is_modifiable()
    #

    print "Testing is_modifiable()..."
    c = kconfiglib.Config("Kconfiglib/tests/Kmodifiable")
    for s in (c["VISIBLE"],
              c["TRISTATE_SELECTED_TO_M"],
              c["VISIBLE_STRING"],
              c["VISIBLE_INT"],
              c["VISIBLE_HEX"]):
        assert_true(s.is_modifiable(),
                    "{0} should be modifiable".format(s.get_name()))
    for s in (c["NOT_VISIBLE"],
              c["SELECTED_TO_Y"],
              c["BOOL_SELECTED_TO_M"],
              c["NOT_VISIBLE_STRING"],
              c["NOT_VISIBLE_INT"],
              c["NOT_VISIBLE_HEX"]):
        assert_false(s.is_modifiable(),
                     "{0} should not be modifiable".format(s.get_name()))

    #
    # get_lower/upper_bound() and get_assignable_values()
    #

    print "Testing get_lower/upper_bound() and get_assignable_values()..."
    c = kconfiglib.Config("Kconfiglib/tests/Kbounds")
    def assert_bounds(sym, lower, upper):
        sym = c[sym]
        low = sym.get_lower_bound()
        high = sym.get_upper_bound()
        assert_true(low == lower and high == upper,
                    "Incorrectly calculated bounds for {0}: {1}-{2}. "
                    "Expected {3}-{4}.".format(sym.get_name(),
                                              low, high, lower, upper))
        # See that we get back the corresponding range from
        # get_assignable_values()
        if low is None:
            vals = sym.get_assignable_values()
            assert_true(vals == [],
                        "get_assignable_values() thinks there should be "
                        "assignable values for {0} ({1}) but not "
                        "get_lower/upper_bound()".format(sym.get_name(), vals))
        else:
            tri_to_int = { "n" : 0, "m" : 1, "y" : 2 }
            bound_range = ["n", "m", "y"][tri_to_int[low] :
                                          tri_to_int[high] + 1]
            assignable_range = sym.get_assignable_values()
            assert_true(bound_range == assignable_range,
                        "get_lower/upper_bound() thinks the range for {0} "
                        "should be {1} while get_assignable_values() thinks "
                        "it should be {2}".format(sym.get_name(), bound_range,
                                                  assignable_range))
    assert_bounds("Y_VISIBLE_BOOL", "n", "y")
    assert_bounds("Y_VISIBLE_TRISTATE", "n", "y")
    assert_bounds("M_VISIBLE_BOOL", "n", "y")
    assert_bounds("M_VISIBLE_TRISTATE", "n", "m")
    assert_bounds("Y_SELECTED_BOOL", None, None)
    assert_bounds("M_SELECTED_BOOL", None, None)
    assert_bounds("Y_SELECTED_TRISTATE", None, None)
    assert_bounds("M_SELECTED_TRISTATE", "m", "y")
    assert_bounds("M_SELECTED_M_VISIBLE_TRISTATE", None, None)
    assert_bounds("STRING", None, None)
    assert_bounds("INT", None, None)
    assert_bounds("HEX", None, None)

    #
    # eval() (Already well exercised. Just test some basics.)
    #

    # TODO: Stricter syntax checking?

    print "Testing eval()..."
    c = kconfiglib.Config("Kconfiglib/tests/Keval")
    def assert_val(expr, val):
        res = c.eval(expr)
        assert_true(res == val,
                    "'{0}' evaluated to {1}, expected {2}".\
                    format(expr, res, val))
    # No modules
    assert_val("n", "n")
    assert_val("m", "n")
    assert_val("y", "y")
    assert_val("M", "y")
    # Modules
    c["MODULES"].set_value("y")
    assert_val("n", "n")
    assert_val("m", "m")
    assert_val("y", "y")
    assert_val("M", "m")
    assert_val("(Y || N) && (m && y)", "m")

    #
    # Text queries
    #

    # TODO: Get rid of extra \n's at end of texts?

    print "Testing various text queries..."
    c = kconfiglib.Config("Kconfiglib/tests/Ktext")
    assert_equals(c["NO_HELP"].get_help(), None)
    assert_equals(c["S"].get_help(), "help for\nS\n")
    assert_equals(c.get_choices()[0].get_help(), "help for\nC\n")
    assert_equals(c.get_comments()[0].get_text(), "a comment")
    assert_equals(c.get_menus()[0].get_title(), "a menu")

    print

def run_compatibility_tests():
    """Runs tests on configurations from the kernel. Tests compability with the
    C implementation by comparing outputs."""

    print "Running compatibility tests...\n"

    # The set of tests that want to run for all architectures in the kernel
    # tree -- currently, all tests. The boolean flag indicates whether .config
    # (generated by the C implementation) should be compared to ._config
    # (generated by us) after each invocation.
    all_arch_tests = [(test_all_no,        True),
                      (test_config_absent, True),
                      (test_all_yes,       True),
                      (test_call_all,      False),
                      # Needs to report success/failure for each arch/defconfig
                      # combo, hence False.
                      (test_defconfig,     False)]

    print "Loading Config instances for all architectures..."
    arch_configs = get_arch_configs()

    for (test_fn, compare_configs) in all_arch_tests:
        print "Resetting all architecture Config instances prior to next test..."
        for arch in arch_configs:
            arch.reset()

        # The test description is taken from the docstring of the corresponding
        # function
        print textwrap.dedent(test_fn.__doc__)

        for conf in arch_configs:
            rm_configs()

            # This should be set correctly for any 'make *config' commands the
            # test might run. SRCARCH is selected automatically from ARCH, so
            # we don't need to set that.
            os.environ["ARCH"] = conf.get_arch()

            test_fn(conf)

            if compare_configs:
                sys.stdout.write("  {0:<14}".format(conf.get_arch()))

                if equal_confs():
                    print "OK"
                else:
                    print "FAIL"
                    fail()

        print ""

    if all_ok():
        print "All OK"
        print nconfigs, "arch/defconfig pairs tested"
    else:
        print "Some tests failed"

def get_arch_configs():
    """Returns a list with Config instances corresponding to all arch
    Kconfigs."""

    # TODO: Could this be made more robust across kernel versions by checking
    # for the existence of particular arches?

    def add_arch(ARCH, res):
        os.environ["SRCARCH"] = archdir
        os.environ["ARCH"] = ARCH
        res.append(kconfiglib.Config(base_dir = "."))

    res = []

    for archdir in os.listdir("arch"):
        if archdir == "h8300":
            # Broken Kconfig as of Linux 2.6.38-rc3
            continue

        if os.path.exists(os.path.join("arch", archdir, "Kconfig")):
            add_arch(archdir, res)
            # Some arches define additional ARCH settings with ARCH != SRCARCH.
            # (Search for "Additional ARCH settings for" in the Makefile.) We
            # test those as well.
            if archdir == "x86":
                add_arch("i386", res)
                add_arch("x86_64", res)
            elif archdir == "sparc":
                add_arch("sparc32", res)
                add_arch("sparc64", res)
            elif archdir == "sh":
                add_arch("sh64", res)
            elif archdir == "tile":
                add_arch("tilepro", res)
                add_arch("tilegx", res)

    # Don't want subsequent 'make *config' commands in tests to see this
    del os.environ["ARCH"]
    del os.environ["SRCARCH"]

    return res

# The weird docstring formatting is to get the format right when we print the
# docstring ourselves
def test_all_no(conf):
    """
    Test if our allnoconfig implementation generates the same .config as
    'make allnoconfig', for all architectures"""

    while True:
        done = True

        for sym in conf:
            # Choices take care of themselves for allnoconf, so we only need to
            # worry about non-choice symbols
            if not sym.is_choice_item():
                lower_bound = sym.get_lower_bound()

                # If we can assign a lower value to the symbol (where "n", "m" and
                # "y" are ordered from lowest to highest), then do so.
                # lower_bound() returns None for symbols whose values cannot
                # (currently) be changed, as well as for non-bool, non-tristate
                # symbols.
                if lower_bound is not None and \
                   kconfiglib.tri_less(lower_bound, sym.calc_value()):

                    sym.set_value(lower_bound)

                    # We just changed the value of some symbol. As this may effect
                    # other symbols, we need to keep looping.
                    done = False

        if done:
            break

    conf.write_config("._config")

    shell("make allnoconfig")

def test_all_yes(conf):
    """
    Test if our allyesconfig implementation generates the same .config as 'make
    allyesconfig', for all architectures"""

    # Get a list of all symbols that are not choice items
    non_choice_syms = [sym for sym in conf.get_symbols() if
                       not sym.is_choice_item()]

    while True:
        done = True

        # Handle symbols outside of choices

        for sym in non_choice_syms:
            upper_bound = sym.get_upper_bound()

            # See corresponding comment for allnoconf implementation
            if upper_bound is not None and \
               kconfiglib.tri_less(sym.calc_value(), upper_bound):
                sym.set_value(upper_bound)
                done = False

        # Handle symbols within choices

        for choice in conf.get_choices():

            # Handle choices whose visibility allow them to be in "y" mode

            if choice.get_visibility() == "y":
                selection = choice.get_selection_from_defaults()
                if selection is not None and \
                   selection is not choice.get_user_selection():
                    selection.set_value("y")
                    done = False

            # Handle choices whose visibility only allow them to be in "m" mode

            elif choice.get_visibility() == "m":
                for sym in choice.get_items():
                    if sym.calc_value() != "m" and \
                       sym.get_upper_bound() != "n":
                        sym.set_value("m")
                        done = False


        if done:
            break

    conf.write_config("._config")

    shell("make allyesconfig")

def test_call_all(conf):
    """
    Call all public methods on all symbols, menus, choices and comments (nearly
    all public methods: some are hard to test like this, but are exercised by
    other tests) for all architectures to make sure we never crash or hang.
    Also do misc. sanity checks."""
    print "  For {0}...".format(conf.get_arch())

    conf.get_arch()
    conf.get_srcarch()
    conf.get_srctree()
    conf.get_config_filename()
    conf.get_defconfig_filename()
    conf.get_top_level_items()
    conf.eval("y && ARCH")

    # Syntax error
    caught_exception = False
    try:
        conf.eval("y && && y")
    except kconfiglib.Kconfig_Syntax_Error:
        caught_exception = True

    if not caught_exception:
        fail("No exception generated for expression with syntax error")

    conf.get_config_header()
    conf.get_base_dir()
    conf.reset()
    conf.get_symbols(False)
    conf.get_mainmenu_text()

    for s in conf.get_symbols():
        s.reset()
        s.calc_value()
        s.calc_default_value()
        s.get_user_value()
        s.get_name()
        s.get_upper_bound()
        s.get_lower_bound()
        s.get_assignable_values()
        s.get_type()
        s.get_visibility()
        s.get_parent()
        s.get_sibling_symbols()
        s.get_sibling_items()
        s.get_referenced_symbols()
        s.get_referenced_symbols(True)
        s.get_selected_symbols()
        s.get_help()
        s.get_config()

        # Check get_ref/def_location() sanity

        if s.is_special():
            if s.is_from_environment():
                # Special symbols from the environment should have define
                # locations
                if s.get_def_locations() == []:
                    fail("The symbol '{0}' is from the environment but "
                         "lacks define locations".format(s.get_name()))
            else:
                # Special symbols that are not from the environment should be
                # defined and have no define locations
                if not s.is_defined():
                    fail("The special symbol '{0}' is not defined".
                         format(s.get_name()))
                if not s.get_def_locations() == []:
                    fail("The special symbol '{0}' has recorded def. "\
                         "locations".format(s.get_name()))
        else:
            # Non-special symbols should have define locations iff they are
            # defined
            if s.is_defined():
                if s.get_def_locations() == []:
                    fail("'{0}' defined but lacks recorded locations".\
                         format(s.get_name()))
            else:
                if s.get_def_locations() != []:
                    fail("'{0}' undefined but has recorded locations".\
                         format(s.get_name()))
                if s.get_ref_locations() == []:
                    fail("'{0}' both undefined and unreferenced".\
                          format(s.get_name()))

        s.get_ref_locations()
        s.is_modifiable()
        s.is_defined()
        s.is_from_environment()
        s.has_ranges()
        s.is_choice_item()
        s.is_choice_selection()
        s.__str__()

    for c in conf.get_choices():
        c.get_name()
        c.get_selection()
        c.get_selection_from_defaults()
        c.get_user_selection()
        c.get_type()
        c.get_name()
        c.get_items()
        c.get_actual_items()
        c.get_parent()
        c.get_referenced_symbols()
        c.get_referenced_symbols(True)
        c.get_def_locations()
        c.get_visibility()
        c.calc_mode()
        c.is_optional()
        c.__str__()

    for m in conf.get_menus():
        m.get_items()
        m.get_symbols(False)
        m.get_symbols(True)
        m.get_depends_on_visibility()
        m.get_visible_if_visibility()
        m.get_title()
        m.get_parent()
        m.get_referenced_symbols()
        m.get_referenced_symbols(True)
        m.get_location()
        m.__str__()

    for c in conf.get_comments():
        c.get_text()
        c.get_parent()
        c.get_referenced_symbols()
        c.get_referenced_symbols(True)
        c.get_location()
        c.__str__()

def test_config_absent(conf):
    """
    Test if kconfiglib generates the same configuration as 'conf' without a
    .config, for each architecture"""
    conf.write_config("._config")
    shell("make alldefconfig")

def test_defconfig(conf):
    """
    Test if kconfiglib generates the same .config as scripts/kconfig/conf for
    each architecture/defconfig pair. This test includes nonsensical groupings
    of arches with defconfigs from other arches (every arch/defconfig
    combination in fact) as this has proven effective in finding obscure bugs.
    For that reason this test takes many hours to run even on fast systems.

    This test appends any failures to a file test_defconfig_fails in the
    root."""
    # TODO: Make it possible to run this test only for valid arch/defconfig
    # combinations for a speedier test run?

    # TODO: Make log file generation optional via argument to kconfigtest.py

    with open("test_defconfig_fails", "a") as fail_log:
        # Collect defconfigs. This could be done once instead, but it's a speedy
        # operation comparatively.

        global nconfigs

        defconfigs = []

        for arch in os.listdir("arch"):
            arch_dir = os.path.join("arch", arch)
            # Some arches have a "defconfig" in the root of their arch/<arch>/
            # directory
            root_defconfig = os.path.join(arch_dir, "defconfig")
            if os.path.exists(root_defconfig):
                defconfigs.append(root_defconfig)
            # Assume all files in the arch/<arch>/configs directory (if it
            # exists) are configurations
            defconfigs_dir = os.path.join(arch_dir, "configs")
            if not os.path.exists(defconfigs_dir):
                continue
            if not os.path.isdir(defconfigs_dir):
                print "Warning: '{0}' is not a directory - skipping"\
                      .format(defconfigs_dir)
                continue
            for dirpath, dirnames, filenames in os.walk(defconfigs_dir):
                for filename in filenames:
                    defconfigs.append(os.path.join(dirpath, filename))

        # Test architecture for each defconfig

        for defconfig in defconfigs:
            rm_configs()

            nconfigs += 1

            conf.load_config(defconfig)
            conf.write_config("._config")
            shell("cp {0} .config".format(defconfig))
            # It would be a bit neater if we could use 'make *_defconfig' here
            # (for example, 'make i386_defconfig' loads
            # arch/x86/configs/i386_defconfig' if ARCH = x86/i386/x86_64), but
            # that wouldn't let us test nonsensical combinations of arches and
            # defconfigs, which is a nice way to find obscure bugs.
            shell("make kconfiglibtestconfig")

            sys.stdout.write("  {0:<14}with {1:<60} ".
                             format(conf.get_arch(), defconfig))

            if equal_confs():
                print "OK"
            else:
                print "FAIL"
                fail()
                fail_log.write("{0}  {1} with {2} did not match\n"
                        .format(time.strftime("%d %b %Y %H:%M:%S",
                                              time.localtime()),
                                conf.get_arch(),
                                defconfig))
                fail_log.flush()

#
# Helper functions
#

def shell(cmd):
    subprocess.Popen(cmd,
                     shell = True,
                     stdout = subprocess.PIPE,
                     stderr = subprocess.PIPE).communicate()

def rm_configs():
    """Delete any old ".config" (generated by the C implementation) and
    "._config" (generated by us), if present."""
    def rm_if_exists(f):
        if os.path.exists(f):
            os.remove(f)

    rm_if_exists(".config")
    rm_if_exists("._config")

def equal_confs():
    with open(".config") as menu_conf:
        l1 = menu_conf.readlines()

    with open("._config") as my_conf:
        l2 = my_conf.readlines()

    # Skip the header generated by 'conf'
    unset_re = r"# CONFIG_(\w+) is not set"
    i = 0
    for line in l1:
        if not line.startswith("#") or \
           re.match(unset_re, line):
            break
        i += 1

    return (l1[i:] == l2)

_all_ok = True

def assert_true(cond, msg):
    """Fails and prints 'msg' if 'conf' is False."""
    if not cond:
        fail(msg)

def assert_false(cond, msg):
    """Fails and prints 'msg' if 'conf' is True."""
    if cond:
        fail(msg)

def assert_equals(x, y):
    """Fails if 'x' does not equal 'y'."""
    if x != y:
        fail("'{0}' does not equal '{1}'".format(x, y))

def fail(msg = None):
    global _all_ok
    if msg is not None:
        print "Fail: " + msg
    _all_ok = False

def all_ok():
    return _all_ok

if __name__ == "__main__":
    run_tests()
