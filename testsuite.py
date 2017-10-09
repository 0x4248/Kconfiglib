# This is the Kconfiglib test suite. It runs selftests on Kconfigs provided by
# us and tests compatibility with the C Kconfig implementation by comparing the
# output of Kconfiglib with the output of the scripts/kconfig/*conf utilities
# for different targets and defconfigs. It should be run from the top-level
# kernel directory with
#
#   $ python Kconfiglib/testsuite.py
#
# Some additional options can be turned on by passing them as arguments. They
# default to off.
#
#  - speedy:
#    Run scripts/kconfig/conf directly instead of using 'make' targets. Makes
#    things a lot faster, but could break if Kconfig files start referencing
#    additional environment variables beyond ARCH, SRCARCH, and KERNELVERSION.
#    Safe as of Linux 4.14-rc3.
#
#  - obsessive:
#    By default, only valid arch/defconfig pairs are tested. In obsessive mode,
#    every arch will be tested with every defconfig. Increases the testing time
#    by an order of magnitude. Occasionally finds (usually obscure) bugs, and I
#    make sure everything passes with it.
#
#  - log:
#    Log timestamped defconfig test failures to the file test_defconfig_fails.
#    Handy in obsessive mode.
#
# For example, this commands runs the test suite in speedy mode with logging
# enabled:
#
#   $ python(3) Kconfiglib/testsuite.py speedy log
#
# pypy works too, and runs most tests much faster than CPython.
#
# All tests should pass. Report regressions to ulfalizer a.t Google's email
# service.

import difflib
import errno
import kconfiglib
import os
import platform
import re
import subprocess
import sys
import textwrap
import time

def shell(cmd):
    with open(os.devnull, "w") as devnull:
        subprocess.call(cmd, shell=True, stdout=devnull, stderr=devnull)

all_passed = True

def fail(msg=None):
    global all_passed
    all_passed = False
    if msg is not None:
        print("Fail: " + msg)

def verify(cond, msg):
    if not cond:
        fail(msg)

def verify_equal(x, y):
    if x != y:
        fail("'{}' does not equal '{}'".format(x, y))

# Assign this to avoid warnings from Kconfiglib. Nothing in the kernel's
# Kconfig files seems to actually look at the value as of 3.7.0-rc8. This is
# only relevant for the test suite, as this will get set by the kernel Makefile
# when using (i)scriptconfig.
os.environ["KERNELVERSION"] = "1"

# Prevent accidental loading of configuration files by removing
# KCONFIG_ALLCONFIG from the environment
os.environ.pop("KCONFIG_ALLCONFIG", None)

speedy = False
obsessive = False
log = False

# Number of arch/defconfig pairs tested so far
nconfigs = 0

def run_tests():
    global speedy, obsessive, log
    for s in sys.argv[1:]:
        if s == "speedy":
            speedy = True
            print("Speedy mode enabled")
        elif s == "obsessive":
            obsessive = True
            print("Obsessive mode enabled")
        elif s == "log":
            log = True
            print("Log mode enabled")
        else:
            print("Unrecognized option '{}'".format(s))
            return

    run_selftests()
    run_compatibility_tests()

def get_items(config, type_):
    items = []
    def rec(node):
        if node is not None:
             if isinstance(node.item, type_):
                 items.append(node.item)
             rec(node.list)
             rec(node.next)
    rec(config.top_menu)
    return items

def get_comments(config):
    items = []
    def rec(node):
        if node is not None:
             if node.item == kconfiglib.COMMENT:
                 items.append(node)
             rec(node.list)
             rec(node.next)
    rec(config.top_menu)
    return items

def get_menus(config):
    items = []
    def rec(node):
        if node is not None:
             if node.item == kconfiglib.MENU:
                 items.append(node)
             rec(node.list)
             rec(node.next)
    rec(config.top_menu)
    return items

def get_choices(config):
    choices = get_items(config, kconfiglib.Choice)
    unique_choices = []
    for choice in choices:
        if choice not in unique_choices:
            unique_choices.append(choice)
    return unique_choices

def get_parent(item):
    if isinstance(item, (kconfiglib.Symbol, kconfiglib.Choice)):
        if not item.nodes:
            return None
        return item.nodes[0].parent.item
    return item.node.parent.item

def get_prompts(item):
    prompts = []
    for node in item.nodes:
        if node.prompt is not None:
            prompts.append(node.prompt[0])
    return prompts

def run_selftests():
    #
    # Common helper functions. These all expect 'c' to hold the current
    # configuration.
    #

    def verify_value(sym_name, val):
        """
        Verifies that a symbol has a particular value.
        """
        sym = c.syms[sym_name]
        verify(sym.value == val,
               'expected {} to have the value "{}", had the value "{}"'
               .format(sym_name, val, sym.value))

    def assign_and_verify_value(sym_name, val, new_val):
        """
        Assigns 'val' to a symbol and verifies that its value becomes
        'new_val'.
        """
        sym = c.syms[sym_name]
        old_val = sym.value
        sym.set_value(val)
        verify(sym.value == new_val,
               'expected {} to have the value "{}" after being assigned the '
               'value "{}". Instead, the value is "{}". The old value was '
               '"{}".'
               .format(sym_name, new_val, val, sym.value, old_val))

    def assign_and_verify(sym_name, user_val):
        """
        Like assign_and_verify_value(), with the expected value being the
        value just set.
        """
        assign_and_verify_value(sym_name, user_val, user_val)

    def assign_and_verify_user_value(sym_name, val, user_val):
        """Assigns a user value to the symbol and verifies the new user
        value."""
        sym = c.syms[sym_name]
        sym_old_user_val = sym.user_value
        sym.set_value(val)
        verify(sym.user_value == user_val,
               "{} should have the user value '{}' after being assigned "
               "the user value '{}'. Instead, the new user value was '{}'. "
               "The old user value was '{}'."
               .format(sym_name, user_val, user_val, sym.user_value,
                       sym_old_user_val))

    #
    # Selftests
    #

    print("Testing tristate comparisons")

    def verify_truth_table(comp_fn, *table):
        for (x, y), expected in zip((("n", "n"), ("n", "m"), ("n", "y"),
                                     ("m", "n"), ("m", "m"), ("m", "y"),
                                     ("y", "n"), ("y", "m"), ("y", "y")),
                                    table):
            verify(comp_fn(x, y) == expected,
                   "expected {} on ('{}', '{}') to be '{}'".
                   format(comp_fn, x, y, expected))

    verify_truth_table(kconfiglib.tri_less,
                       False, True, True,
                       False, False, True,
                       False, False, False)

    verify_truth_table(kconfiglib.tri_less_eq,
                       True, True, True,
                       False, True, True,
                       False, False, True)

    verify_truth_table(kconfiglib.tri_greater,
                       False, False, False,
                       True, False, False,
                       True, True, False)

    verify_truth_table(kconfiglib.tri_greater_eq,
                       True, False, False,
                       True, True, False,
                       True, True, True)


    print("Testing string literal (constant symbol) lexing")

    # Dummy empty configuration just to get a Config object
    c = kconfiglib.Config("Kconfiglib/tests/empty")

    def verify_string_lex(s, res):
        """
        Verifies that the string (constant symbol) token 'res' is produced from
        lexing 's'. Strips the first and last characters from 's' so that
        readable raw strings can be used as input
        """
        s = s[1:-1]
        token = c._tokenize(s, for_eval = True).next()
        verify(token == res,
               "expected {} to produced the string token {}, produced {}"
               .format(s, token, res))

    verify_string_lex(r""" "" """, "")
    verify_string_lex(r""" '' """, "")

    verify_string_lex(r""" "a" """, "a")
    verify_string_lex(r""" 'a' """, "a")
    verify_string_lex(r""" "ab" """, "ab")
    verify_string_lex(r""" 'ab' """, "ab")
    verify_string_lex(r""" "abc" """, "abc")
    verify_string_lex(r""" 'abc' """, "abc")

    verify_string_lex(r""" "'" """, "'")
    verify_string_lex(r""" '"' """, '"')

    verify_string_lex(r""" "\"" """, '"')
    verify_string_lex(r""" '\'' """, "'")

    verify_string_lex(r""" "\"\"" """, '""')
    verify_string_lex(r""" '\'\'' """, "''")

    verify_string_lex(r""" "\'" """, "'")
    verify_string_lex(r""" '\"' """, '"')

    verify_string_lex(r""" "\\" """, "\\")
    verify_string_lex(r""" '\\' """, "\\")

    verify_string_lex(r""" "\a\\'\b\c\"'d" """, 'a\\\'bc"\'d')
    verify_string_lex(r""" '\a\\"\b\c\'"d' """, "a\\\"bc'\"d")

    def verify_string_bad(s):
        """
        Verifies that tokenizing 's' throws a KconfigSyntaxError. Strips the
        first and last characters from 's' so we can use readable raw strings
        as input.
        """
        s = s[1:-1]
        try:
            c._tokenize(s, for_eval = True)
        except kconfiglib.KconfigSyntaxError:
            pass
        else:
            fail("expected tokenization of {} to fail, didn't".format(s))

    verify_string_bad(r""" " """)
    verify_string_bad(r""" ' """)
    verify_string_bad(r""" "' """)
    verify_string_bad(r""" '" """)
    verify_string_bad(r""" "\" """)
    verify_string_bad(r""" '\' """)
    verify_string_bad(r""" "foo """)
    verify_string_bad(r""" 'foo """)

    # TODO: Kmodifiable gone, test assignable


    print("Testing expression evaluation")

    c = kconfiglib.Config("Kconfiglib/tests/Keval")

    def verify_eval(expr, val):
        res = c.eval_string(expr)
        verify(res == val,
               "'{}' evaluated to {}, expected {}".format(expr, res, val))

    # No modules
    verify_eval("n", "n")
    verify_eval("m", "n")
    verify_eval("y", "y")
    verify_eval("'n'", "n")
    verify_eval("'m'", "n")
    verify_eval("'y'", "y")
    verify_eval("M", "y")

    # Modules
    c.syms["MODULES"].set_value("y")
    verify_eval("n", "n")
    verify_eval("m", "m")
    verify_eval("y", "y")
    verify_eval("'n'", "n")
    verify_eval("'m'", "m")
    verify_eval("'y'", "y")
    verify_eval("M", "m")
    verify_eval("(Y || N) && (m && y)", "m")

    # Non-bool/non-tristate symbols are always "n" in a tristate sense
    verify_eval("Y_STRING", "n")
    verify_eval("Y_STRING || m", "m")

    # As are all constants besides "y" and "m"
    verify_eval('"foo"', "n")
    verify_eval('"foo" || "bar"', "n")
    verify_eval('"foo" || m', "m")

    # Test equality for symbols

    verify_eval("N = N", "y")
    verify_eval("N = n", "y")
    verify_eval("N = 'n'", "y")
    verify_eval("N != N", "n")
    verify_eval("N != n", "n")
    verify_eval("N != 'n'", "n")

    verify_eval("M = M", "y")
    verify_eval("M = m", "y")
    verify_eval("M = 'm'", "y")
    verify_eval("M != M", "n")
    verify_eval("M != m", "n")
    verify_eval("M != 'm'", "n")

    verify_eval("Y = Y", "y")
    verify_eval("Y = y", "y")
    verify_eval("Y = 'y'", "y")
    verify_eval("Y != Y", "n")
    verify_eval("Y != y", "n")
    verify_eval("Y != 'y'", "n")

    verify_eval("N != M", "y")
    verify_eval("N != Y", "y")
    verify_eval("M != Y", "y")

    verify_eval("Y_STRING = y", "y")
    verify_eval("Y_STRING = 'y'", "y")
    verify_eval('FOO_BAR_STRING = "foo bar"', "y")
    verify_eval('FOO_BAR_STRING != "foo bar baz"', "y")
    verify_eval('INT_37 = 37', "y")
    verify_eval("INT_37 = '37'", "y")
    verify_eval('HEX_0X37 = 0x37', "y")
    verify_eval("HEX_0X37 = '0x37'", "y")

    # These should also hold after 31847b67 (kconfig: allow use of relations
    # other than (in)equality)
    verify_eval("HEX_0X37 = '0x037'", "y")
    verify_eval("HEX_0X37 = '0x0037'", "y")

    # Constant symbol comparisons
    verify_eval('"foo" != "bar"', "y")
    verify_eval('"foo" = "bar"', "n")
    verify_eval('"foo" = "foo"', "y")

    # Undefined symbols get their name as their value
    c.disable_warnings()
    verify_eval("'not_defined' = not_defined", "y")
    verify_eval("not_defined_2 = not_defined_2", "y")
    verify_eval("not_defined_1 != not_defined_2", "y")

    # Test less than/greater than

    # Basic evaluation
    verify_eval("INT_37 < 38", "y")
    verify_eval("38 < INT_37", "n")
    verify_eval("INT_37 < '38'", "y")
    verify_eval("'38' < INT_37", "n")
    verify_eval("INT_37 < 138", "y")
    verify_eval("138 < INT_37", "n")
    verify_eval("INT_37 < '138'", "y")
    verify_eval("'138' < INT_37", "n")
    verify_eval("INT_37 < -138", "n")
    verify_eval("-138 < INT_37", "y")
    verify_eval("INT_37 < '-138'", "n")
    verify_eval("'-138' < INT_37", "y")
    verify_eval("INT_37 < 37", "n")
    verify_eval("37 < INT_37", "n")
    verify_eval("INT_37 < 36", "n")
    verify_eval("36 < INT_37", "y")

    # Different formats in comparison
    verify_eval("INT_37 < 0x26", "y") # 38
    verify_eval("INT_37 < 0x25", "n") # 37
    verify_eval("INT_37 < 0x24", "n") # 36
    verify_eval("HEX_0X37 < 56", "y") # 0x38
    verify_eval("HEX_0X37 < 55", "n") # 0x37
    verify_eval("HEX_0X37 < 54", "n") # 0x36

    # Other int comparisons
    verify_eval("INT_37 <= 38", "y")
    verify_eval("INT_37 <= 37", "y")
    verify_eval("INT_37 <= 36", "n")
    verify_eval("INT_37 >  38", "n")
    verify_eval("INT_37 >  37", "n")
    verify_eval("INT_37 >  36", "y")
    verify_eval("INT_37 >= 38", "n")
    verify_eval("INT_37 >= 37", "y")
    verify_eval("INT_37 >= 36", "y")

    # Other hex comparisons
    verify_eval("HEX_0X37 <= 0x38", "y")
    verify_eval("HEX_0X37 <= 0x37", "y")
    verify_eval("HEX_0X37 <= 0x36", "n")
    verify_eval("HEX_0X37 >  0x38", "n")
    verify_eval("HEX_0X37 >  0x37", "n")
    verify_eval("HEX_0X37 >  0x36", "y")
    verify_eval("HEX_0X37 >= 0x38", "n")
    verify_eval("HEX_0X37 >= 0x37", "y")
    verify_eval("HEX_0X37 >= 0x36", "y")

    # A hex holding a value without a "0x" prefix should still be treated as
    # hexadecimal
    verify_eval("HEX_37 < 0x38", "y")
    verify_eval("HEX_37 < 0x37", "n")
    verify_eval("HEX_37 < 0x36", "n")

    # Symbol comparisons
    verify_eval("INT_37   <  HEX_0X37", "y")
    verify_eval("INT_37   >  HEX_0X37", "n")
    verify_eval("HEX_0X37 <  INT_37  ", "n")
    verify_eval("HEX_0X37 >  INT_37  ", "y")
    verify_eval("INT_37   <  INT_37  ", "n")
    verify_eval("INT_37   <= INT_37  ", "y")
    verify_eval("INT_37   >  INT_37  ", "n")
    verify_eval("INT_37   <= INT_37  ", "y")

    # Strings compare lexicographically
    verify_eval("'aa' < 'ab'", "y")
    verify_eval("'aa' > 'ab'", "n")
    verify_eval("'ab' < 'aa'", "n")
    verify_eval("'ab' > 'aa'", "y")

    # If one operand is numeric and the other not a valid number, we get 'n'
    verify_eval("INT_37 <  oops  ", "n")
    verify_eval("INT_37 <= oops  ", "n")
    verify_eval("INT_37 >  oops  ", "n")
    verify_eval("INT_37 >= oops  ", "n")
    verify_eval("oops   <  INT_37", "n")
    verify_eval("oops   <= INT_37", "n")
    verify_eval("oops   >  INT_37", "n")
    verify_eval("oops   >= INT_37", "n")

    def verify_eval_bad(expr):
        try:
            c.eval_string(expr)
        except kconfiglib.KconfigSyntaxError:
            pass
        else:
            fail('expected eval_string("{}") to throw KconfigSyntaxError, ' \
                 'didn\'t'.format(expr))

    # The C implementation's parser can be pretty lax about syntax. Kconfiglib
    # sometimes needs to emulate that. Verify that some bad stuff throws
    # KconfigSyntaxError at least.
    verify_eval_bad("")
    verify_eval_bad("&")
    verify_eval_bad("|")
    verify_eval_bad("!")
    verify_eval_bad("(")
    verify_eval_bad(")")
    verify_eval_bad("=")
    verify_eval_bad("(X")
    verify_eval_bad("X &&")
    verify_eval_bad("&& X")
    verify_eval_bad("X ||")
    verify_eval_bad("|| X")


    print("Testing Symbol.__str__()")

    def verify_str(item, s):
        verify_equal(str(item), s[1:])

    c = kconfiglib.Config("Kconfiglib/tests/Kstr", warn=False)

    verify_str(c.syms["UNDEFINED"], """
""")

    verify_str(c.syms["BASIC_NO_PROMPT"], """
config BASIC_NO_PROMPT
	bool
	help
	  blah blah
	  
	    blah blah blah
	  
	   blah
""")

    verify_str(c.syms["BASIC_PROMPT"], """
config BASIC_PROMPT
	bool
	prompt "basic"
""")

    verify_str(c.syms["ADVANCED"], """
config ADVANCED
	tristate
	prompt "prompt" if DEP
	default DEFAULT_1
	default DEFAULT_2 if DEP
	select SELECTED_1
	select SELECTED_2 if DEP
	imply IMPLIED_1
	imply IMPLIED_2 if DEP
	help
	  first help text

config ADVANCED
	prompt "prompt 2"

menuconfig ADVANCED
	prompt "prompt 3" if DEP2

config ADVANCED
	help
	  second help text
""")

    verify_str(c.syms["STRING"], """
config STRING
	string
	default "foo"
	default "bar" if DEP
	default STRING2
	default STRING3 if DEP
""")

    verify_str(c.syms["INT"], """
config INT
	int
	range 1 2
	range FOO BAR
	range BAZ QAZ if DEP
""")

    # We still hardcode the modules symbol. Otherwise OPTIONS would have made
    # more sense as a name here.
    verify_str(c.syms["MODULES"], """
config MODULES
	option modules
""")

    verify_str(c.syms["OPTIONS"], """
config OPTIONS
	option allnoconfig_y
	option defconfig_list
	option env="ENV"
""")

    print("Testing Choice.__str__()")

    verify_str(c.named_choices["CHOICE"], """
choice CHOICE
	tristate
	prompt "foo"
	default CHOICE_1
	default CHOICE_2 if dep
""")

    verify_str(c.named_choices["CHOICE"].nodes[0].next.item, """
choice
	tristate
	prompt "no name"
""")


    print("Testing Symbol.__repr__()")

    def verify_repr(item, s):
        verify_equal(repr(item) + "\n", s[1:])

    c = kconfiglib.Config("Kconfiglib/tests/Krepr", warn=False)

    verify_repr(c.syms["UNDEFINED"], """
<symbol UNDEFINED, unknown, value "UNDEFINED", visibility n, direct deps n, 0 menu nodes>
""")

    verify_repr(c.syms["BASIC"], """
<symbol BASIC, bool, value "y", visibility n, direct deps y, 1 menu node>
""")

    verify_repr(c.syms["VISIBLE"], """
<symbol VISIBLE, bool, value "n", visibility y, direct deps y, 1 menu node>
""")

    verify_repr(c.syms["DIR_DEP_N"], """
<symbol DIR_DEP_N, unknown, value "DIR_DEP_N", visibility n, direct deps n, 1 menu node>
""")

    verify_repr(c.syms["OPTIONS"], """
<symbol OPTIONS, unknown, value "OPTIONS", visibility n, allnoconfig_y, is the defconfig_list symbol, from environment variable ENV, direct deps y, 1 menu node>
""")

    verify_repr(c.syms["MULTI_DEF"], """
<symbol MULTI_DEF, unknown, value "MULTI_DEF", visibility n, direct deps y, 2 menu nodes>
""")

    verify_repr(c.syms["CHOICE_1"], """
<symbol CHOICE_1, tristate, value "n", visibility y, choice symbol, direct deps y, 1 menu node>
""")

    verify_repr(c.syms["MODULES"], """
<symbol MODULES, bool, value "y", visibility n, is the modules symbol, direct deps y, 1 menu node>
""")



    print("Testing Choice.__repr__()")

    verify_repr(c.named_choices["CHOICE"], """
<choice CHOICE, tristate, mode m, visibility y, 1 menu node>
""")

    c.named_choices["CHOICE"].set_value("y")

    verify_repr(c.named_choices["CHOICE"], """
<choice CHOICE, tristate, mode y, visibility y, CHOICE_1 selected, 1 menu node>
""")

    c.syms["CHOICE_2"].set_value("y")

    verify_repr(c.named_choices["CHOICE"], """
<choice CHOICE, tristate, mode y, visibility y, CHOICE_2 selected, 1 menu node>
""")

    verify_repr(c.syms["CHOICE_HOOK"].nodes[0].next.item, """
<choice, tristate, mode n, visibility n, optional, 1 menu node>
""")


    print("Testing MenuNode.__repr__()")

    verify_repr(c.syms["BASIC"].nodes[0], """
<menu node for symbol BASIC, Kconfiglib/tests/Krepr:9, deps y, has help, has next>
""")

    verify_repr(c.syms["DIR_DEP_N"].nodes[0], """
<menu node for symbol DIR_DEP_N, Kconfiglib/tests/Krepr:17, deps n, has next>
""")

    verify_repr(c.syms["MULTI_DEF"].nodes[0], """
<menu node for symbol MULTI_DEF, Kconfiglib/tests/Krepr:25, deps y, has next>
""")

    verify_repr(c.syms["MULTI_DEF"].nodes[1], """
<menu node for symbol MULTI_DEF, Kconfiglib/tests/Krepr:26, deps y, has next>
""")

    verify_repr(c.syms["MENUCONFIG"].nodes[0], """
<menu node for symbol MENUCONFIG, Kconfiglib/tests/Krepr:28, is menuconfig, deps y, has next>
""")

    verify_repr(c.named_choices["CHOICE"].nodes[0], """
<menu node for choice CHOICE, Kconfiglib/tests/Krepr:30, prompt "choice" (visibility y), deps y, has child, has next>
""")

    verify_repr(c.syms["CHOICE_HOOK"].nodes[0].next, """
<menu node for choice, Kconfiglib/tests/Krepr:43, prompt "choice" (visibility n), deps y, has next>
""")

    verify_repr(c.syms["NO_VISIBLE_IF_HOOK"].nodes[0].next, """
<menu node for menu, Kconfiglib/tests/Krepr:50, prompt "no visible if" (visibility y), deps y, 'visible if' deps y, has next>
""")

    verify_repr(c.syms["VISIBLE_IF_HOOK"].nodes[0].next, """
<menu node for menu, Kconfiglib/tests/Krepr:55, prompt "visible if" (visibility y), deps y, 'visible if' deps m, has next>
""")

    verify_repr(c.syms["COMMENT_HOOK"].nodes[0].next, """
<menu node for comment, Kconfiglib/tests/Krepr:61, prompt "comment" (visibility y), deps y>
""")


    print("Testing Config.__repr__()")

    verify_repr(c, """
<configuration with 15 symbols, main menu prompt "Linux Kernel Configuration", srctree not set, config symbol prefix "CONFIG_", warnings disabled, undef. symbol assignment warnings disabled>
""")

    os.environ["srctree"] = "srctree value"
    os.environ["CONFIG_"] = "CONFIG_ value"

    c = kconfiglib.Config("Kconfiglib/tests/Krepr", warn=False)
    c.enable_warnings()
    c.enable_undef_warnings()

    verify_repr(c, """
<configuration with 15 symbols, main menu prompt "Linux Kernel Configuration", srctree "srctree value", config symbol prefix "CONFIG_ value", warnings enabled, undef. symbol assignment warnings enabled>
""")

    os.environ.pop("srctree", None)
    os.environ.pop("CONFIG_", None)


    print("Testing tricky help strings")

    c = kconfiglib.Config("Kconfiglib/tests/Khelp")

    def verify_help(node, s):
        verify_equal(node.help, s[1:])

    verify_help(c.syms["TWO_HELP_STRINGS"].nodes[0], """
first help string
""")

    verify_help(c.syms["TWO_HELP_STRINGS"].nodes[1], """
second help string
""")

    verify_help(c.syms["NO_BLANK_AFTER_HELP"].nodes[0], """
help for
NO_BLANK_AFTER_HELP
""")

    verify_help(c.named_choices["CHOICE_HELP"].nodes[0], """
help for
CHOICE_HELP
""")

    verify_help(c.syms["HELP_TERMINATED_BY_COMMENT"].nodes[0], """
a
b
c
""")

    verify_help(c.syms["TRICKY_HELP"].nodes[0], """
a
 b
  c

 d
  e
   f


g
 h
  i
""")


    print("Testing locations and 'source'")

    def verify_locations(nodes, *expected_locs):
        verify(len(nodes) == len(expected_locs),
               "Wrong number of locations for " + repr(nodes))

        for node, expected_loc in zip(nodes, expected_locs):
            node_loc = "{}:{}".format(node.filename, node.linenr)
            verify(node_loc == expected_loc,
                   "expected {} to have the location {}, had the location {}"
                   .format(repr(node), expected_loc, node_loc))

    # Expanded in the 'source' statement in Klocation
    os.environ["EXPANDED_FROM_ENV"] = "tests"
    os.environ["srctree"] = "Kconfiglib/"

    c = kconfiglib.Config("tests/Klocation")

    os.environ.pop("EXPANDED_FROM_ENV", None)
    os.environ.pop("srctree", None)

    verify_locations(c.syms["SINGLE_DEF"].nodes, "tests/Klocation:4")

    verify_locations(c.syms["MULTI_DEF"].nodes,
      "tests/Klocation:6",
      "tests/Klocation:16",
      "tests/Klocation_included:3")

    verify_locations(c.named_choices["CHOICE"].nodes,
                     "tests/Klocation_included:5")

    verify_locations([c.syms["MENU_HOOK"].nodes[0].next],
                     "tests/Klocation_included:10")

    verify_locations([c.syms["COMMENT_HOOK"].nodes[0].next],
                     "tests/Klocation_included:15")


    print("Testing visibility")

    c = kconfiglib.Config("Kconfiglib/tests/Kvisibility")

    def verify_visibility(item, no_module_vis, module_vis):
        c.syms["MODULES"].set_value("n")
        verify(item.visibility == no_module_vis,
               "expected {} to have visibility {} without modules, had "
               "visibility {}".
               format(repr(item), no_module_vis, item.visibility))

        c.syms["MODULES"].set_value("y")
        verify(item.visibility == module_vis,
               "expected {} to have visibility {} with modules, had "
               "visibility {}".
               format(repr(item), module_vis, item.visibility))

    # Symbol visibility

    verify_visibility(c.syms["NO_PROMPT"],     "n", "n")
    verify_visibility(c.syms["BOOL_N"],        "n", "n")
    verify_visibility(c.syms["BOOL_M"],        "n", "y")
    verify_visibility(c.syms["BOOL_MOD"],      "y", "y")
    verify_visibility(c.syms["BOOL_Y"],        "y", "y")
    verify_visibility(c.syms["TRISTATE_M"],    "n", "m")
    verify_visibility(c.syms["TRISTATE_MOD"],  "y", "m")
    verify_visibility(c.syms["TRISTATE_Y"],    "y", "y")
    verify_visibility(c.syms["BOOL_IF_N"],     "n", "n")
    verify_visibility(c.syms["BOOL_IF_M"],     "n", "y")
    verify_visibility(c.syms["BOOL_IF_Y"],     "y", "y")
    verify_visibility(c.syms["BOOL_MENU_N"],   "n", "n")
    verify_visibility(c.syms["BOOL_MENU_M"],   "n", "y")
    verify_visibility(c.syms["BOOL_MENU_Y"],   "y", "y")
    verify_visibility(c.syms["BOOL_CHOICE_N"], "n", "n")

    # Non-tristate symbols in tristate choices are only visible if the choice
    # is in "y" mode
    verify_visibility(c.syms["BOOL_CHOICE_M"], "n", "n")

    # Tristate choices start out in "m" mode. When running without modules,
    # their type gets adjusted to bool.
    verify_visibility(c.syms["BOOL_CHOICE_Y"], "y", "n")

    c.syms["TRISTATE_CHOICE_M"].set_value("y")
    c.syms["TRISTATE_CHOICE_Y"].set_value("y")

    # Still limited by the visibility of the choice
    verify_visibility(c.syms["BOOL_CHOICE_M"], "n", "n")

    # This one should become visible now
    verify_visibility(c.syms["BOOL_CHOICE_Y"], "y", "y")

    verify_visibility(c.syms["TRISTATE_IF_N"],     "n", "n")
    verify_visibility(c.syms["TRISTATE_IF_M"],     "n", "m")
    verify_visibility(c.syms["TRISTATE_IF_Y"],     "y", "y")
    verify_visibility(c.syms["TRISTATE_MENU_N"],   "n", "n")
    verify_visibility(c.syms["TRISTATE_MENU_M"],   "n", "m")
    verify_visibility(c.syms["TRISTATE_MENU_Y"],   "y", "y")
    verify_visibility(c.syms["TRISTATE_CHOICE_N"], "n", "n")
    verify_visibility(c.syms["TRISTATE_CHOICE_M"], "n", "m")
    verify_visibility(c.syms["TRISTATE_CHOICE_Y"], "y", "y")

    verify_visibility(c.named_choices["BOOL_CHOICE_N"],     "n", "n")
    verify_visibility(c.named_choices["BOOL_CHOICE_M"],     "n", "y")
    verify_visibility(c.named_choices["BOOL_CHOICE_Y"],     "y", "y")
    verify_visibility(c.named_choices["TRISTATE_CHOICE_N"], "n", "n")
    verify_visibility(c.named_choices["TRISTATE_CHOICE_M"], "n", "m")
    verify_visibility(c.named_choices["TRISTATE_CHOICE_Y"], "y", "y")

    verify_visibility(c.named_choices["TRISTATE_CHOICE_IF_M_AND_Y"],
                      "n", "m")
    verify_visibility(c.named_choices["TRISTATE_CHOICE_MENU_N_AND_Y"],
                      "n", "n")

    # Menu visibility

    def verify_menu_visibility(menu, no_module_vis, module_vis):
        c["MODULES"].set_value("n")
        menu_vis = kconfiglib.eval_expr(menu.node.dep)
        verify(menu_vis == no_module_vis,
               "menu \"{}\" should have visibility '{}' without modules, "
               "has visibility '{}'"
               .format(menu.title, no_module_vis, menu_vis))

        c["MODULES"].set_value("y")
        menu_vis = kconfiglib.eval_expr(menu.node.dep)
        verify(menu_vis == module_vis,
               "menu \"{}\" should have visibility '{}' with modules, "
               "has visibility '{}'".
               format(menu.title, module_vis, menu_vis))

    # TODO: does this make sense anymore?

    #menu_n, menu_m, menu_y, menu_if_n, menu_if_m, menu_if_y, \
    #  menu_if_m_and_y = get_menus(c)[5:-5]

    #verify_menu_visibility(menu_n, "n", "n")
    #verify_menu_visibility(menu_m, "n", "m")
    #verify_menu_visibility(menu_y, "y", "y")
    #verify_menu_visibility(menu_if_n, "n", "n")
    #verify_menu_visibility(menu_if_m, "n", "m")
    #verify_menu_visibility(menu_if_y, "y", "y")
    #verify_menu_visibility(menu_if_m_and_y, "n", "m")

    # Menu 'visible if' visibility

    menu_visible_if_n, menu_visible_if_m, menu_visible_if_y, \
      menu_visible_if_m_2 = get_menus(c)[13:]

    def verify_visible_if_visibility(menu, no_module_vis, module_vis):
        c["MODULES"].set_value("n")
        menu_vis = menu.get_visible_if_visibility()
        verify(menu_vis == no_module_vis,
               "menu \"{}\" should have 'visible if' visibility '{}' "
               "without modules, has 'visible if' visibility '{}'".
               format(menu.title, no_module_vis, menu_vis))

        c["MODULES"].set_value("y")
        menu_vis = menu.get_visible_if_visibility()
        verify(menu_vis == module_vis,
               "menu \"{}\" should have 'visible if' visibility '{}' "
               "with modules, has 'visible if' visibility '{}'".
               format(menu.title, module_vis, menu_vis))

    # TODO: verify the visible if stuff after unclassing

    # Ordinary visibility should not affect 'visible if' visibility
    #verify_visible_if_visibility(menu_n, "y", "y")
    #verify_visible_if_visibility(menu_if_n, "y", "y")
    #verify_visible_if_visibility(menu_m, "y", "y")
    #verify_visible_if_visibility(menu_if_m, "y", "y")

    #verify_visible_if_visibility(menu_visible_if_n, "n", "n")
    #verify_visible_if_visibility(menu_visible_if_m, "n", "m")
    #verify_visible_if_visibility(menu_visible_if_y, "y", "y")
    #verify_visible_if_visibility(menu_visible_if_m_2, "n", "m")

    # Verify that 'visible if' visibility gets propagated to prompts
    verify_visibility(c.syms["VISIBLE_IF_N"], "n", "n")
    verify_visibility(c.syms["VISIBLE_IF_M"], "n", "m")
    verify_visibility(c.syms["VISIBLE_IF_Y"], "y", "y")
    verify_visibility(c.syms["VISIBLE_IF_M_2"], "n", "m")

    # Comment visibility

    def verify_comment_visibility(comment, no_module_vis, module_vis):
        c["MODULES"].set_value("n")
        # TODO: uninternalize
        comment_vis = kconfiglib.eval_expr(comment.node.dep)
        verify(comment_vis == no_module_vis,
               "comment \"{}\" should have visibility '{}' without "
               "modules, has visibility '{}'".
               format(comment.text, no_module_vis, comment_vis))

        c["MODULES"].set_value("y")
        comment_vis = kconfiglib.eval_expr(comment.node.dep)
        verify(comment_vis == module_vis,
               "comment \"{}\" should have visibility '{}' with "
               "modules, has visibility '{}'".
               format(comment.text, module_vis, comment_vis))

    # TODO: verify the visibility stuff for comments

    #comment_n, comment_m, comment_y, comment_if_n, comment_if_m, \
    #  comment_if_y, comment_m_nested = get_comments(c)

    #verify_comment_visibility(comment_n, "n", "n")
    #verify_comment_visibility(comment_m, "n", "m")
    #verify_comment_visibility(comment_y, "y", "y")
    #verify_comment_visibility(comment_if_n, "n", "n")
    #verify_comment_visibility(comment_if_m, "n", "m")
    #verify_comment_visibility(comment_if_y, "y", "y")
    #verify_comment_visibility(comment_m_nested, "n", "m")

    # Verify that string/int/hex symbols with m visibility accept a user value

    assign_and_verify("STRING_m", "foo bar")
    assign_and_verify("INT_m", "123")
    assign_and_verify("HEX_m", "0x123")

    #
    # Object relations
    #

    c = kconfiglib.Config("Kconfiglib/tests/Krelation")

    UNDEFINED, A, B, C, D, E, F, G, H, I = \
        c.syms["UNDEFINED"], c.syms["A"], c.syms["B"], c.syms["C"], \
        c.syms["D"], c.syms["E"], c.syms["F"], c.syms["G"], c.syms["H"], \
        c.syms["I"]
    choice_1, choice_2 = get_choices(c)

    # TODO: test new prompts
    #verify([menu.title for menu in get_menus(c)[1:]] ==
    #       ["m1", "m2", "m3", "m4"],
    #       "menu ordering is broken")
    #menu_1, menu_2, menu_3, menu_4 = get_menus(c)[1:]

    print("Testing object relations...")

    # TODO: check parents for menus

    verify(get_parent(UNDEFINED) is None,
           "Undefined symbols should have no parent")
    # TODO: update this test (should be the main menu)
    # TODO: test parents when automatic menus are involved
    #verify(A.get_parent() is None, "A should not have a parent")
    verify(get_parent(B) is choice_1, "B's parent should be the first choice")
    # TODO: no longer true due to auto menus
    #verify(get_parent(C) is choice_1, "C's parent should be the first choice")
    #verify(get_parent(E) is menu_1, "E's parent should be the first menu")
    # TODO: update this test
    #verify(E.get_parent().get_parent() is None,
    #       "E's grandparent should be None")
    verify(get_parent(G) is choice_2,
           "G's parent should be the second choice")
    #verify(get_parent(get_parent(G)) is menu_2,
    #       "G's grandparent should be the second menu")

    # TODO: test parents of comments
    # TODO: test top node

    #
    # hex/int ranges
    #

    print("Testing hex/int ranges...")

    c = kconfiglib.Config("Kconfiglib/tests/Krange")

    for sym_name in "HEX_NO_RANGE", "INT_NO_RANGE", "HEX_40", "INT_40":
        sym = c.syms[sym_name]
        verify(not sym.ranges,
               "{} should not have ranges".format(sym_name))

    for sym_name in "HEX_ALL_RANGES_DISABLED", "INT_ALL_RANGES_DISABLED", \
                    "HEX_RANGE_10_20_LOW_DEFAULT", \
                    "INT_RANGE_10_20_LOW_DEFAULT":
        sym = c.syms[sym_name]
        verify(sym.ranges, "{} should have ranges".format(sym_name))

    # hex/int symbols without defaults should get no default value
    verify_value("HEX_NO_RANGE", "")
    verify_value("INT_NO_RANGE", "")
    # And neither if all ranges are disabled
    verify_value("HEX_ALL_RANGES_DISABLED", "")
    verify_value("INT_ALL_RANGES_DISABLED", "")
    # Make sure they are assignable though, and test that the form of the user
    # value is reflected in the value for hex symbols
    assign_and_verify("HEX_NO_RANGE", "0x123")
    assign_and_verify("HEX_NO_RANGE", "123")
    assign_and_verify("INT_NO_RANGE", "123")

    # Defaults outside of the valid range should be clamped
    verify_value("HEX_RANGE_10_20_LOW_DEFAULT", "0x10")
    verify_value("HEX_RANGE_10_20_HIGH_DEFAULT", "0x20")
    verify_value("INT_RANGE_10_20_LOW_DEFAULT", "10")
    verify_value("INT_RANGE_10_20_HIGH_DEFAULT", "20")
    # Defaults inside the valid range should be preserved. For hex symbols,
    # they should additionally use the same form as in the assignment.
    verify_value("HEX_RANGE_10_20_OK_DEFAULT", "0x15")
    verify_value("HEX_RANGE_10_20_OK_DEFAULT_ALTERNATE", "15")
    verify_value("INT_RANGE_10_20_OK_DEFAULT", "15")

    # hex/int symbols with no defaults but valid ranges should default to the
    # lower end of the range if it's > 0
    verify_value("HEX_RANGE_10_20", "0x10")
    verify_value("HEX_RANGE_0_10", "")
    verify_value("INT_RANGE_10_20", "10")
    verify_value("INT_RANGE_0_10", "")
    verify_value("INT_RANGE_NEG_10_10", "")

    # User values and dependent ranges

    def verify_range(sym_name, low, high, default):
        """Tests that the values in the range 'low'-'high' can be assigned, and
        that assigning values outside this range reverts the value back to
        'default' (None if it should revert back to "")."""
        is_hex = (c.syms[sym_name].type == kconfiglib.HEX)
        for i in range(low, high + 1):
            assign_and_verify_user_value(sym_name, str(i), str(i))
            if is_hex:
                # The form of the user value should be preserved for hex
                # symbols
                assign_and_verify_user_value(sym_name, hex(i), hex(i))

        # Verify that assigning a user value just outside the range causes
        # defaults to be used

        if default is None:
            default_str = ""
        else:
            default_str = hex(default) if is_hex else str(default)

        if is_hex:
            too_low_str = hex(low - 1)
            too_high_str = hex(high + 1)
        else:
            too_low_str = str(low - 1)
            too_high_str = str(high + 1)

        assign_and_verify_value(sym_name, too_low_str, default_str)
        assign_and_verify_value(sym_name, too_high_str, default_str)

    verify_range("HEX_RANGE_10_20_LOW_DEFAULT",  0x10, 0x20,  0x10)
    verify_range("HEX_RANGE_10_20_HIGH_DEFAULT", 0x10, 0x20,  0x20)
    verify_range("HEX_RANGE_10_20_OK_DEFAULT",   0x10, 0x20,  0x15)

    verify_range("INT_RANGE_10_20_LOW_DEFAULT",  10,   20,    10)
    verify_range("INT_RANGE_10_20_HIGH_DEFAULT", 10,   20,    20)
    verify_range("INT_RANGE_10_20_OK_DEFAULT",   10,   20,    15)

    verify_range("HEX_RANGE_10_20",              0x10, 0x20,  0x10)
    verify_range("HEX_RANGE_0_10",               0x0,  0x10,  None)

    verify_range("INT_RANGE_10_20",              10,  20,     10)
    verify_range("INT_RANGE_0_10",               0,   10,     None)
    verify_range("INT_RANGE_NEG_10_10",          -10, 10,     None)

    # Dependent ranges

    verify_value("HEX_40", "40")
    verify_value("INT_40", "40")

    c.syms["HEX_RANGE_10_20"].unset_value()
    c.syms["INT_RANGE_10_20"].unset_value()
    verify_value("HEX_RANGE_10_40_DEPENDENT", "0x10")
    verify_value("INT_RANGE_10_40_DEPENDENT", "10")
    c.syms["HEX_RANGE_10_20"].set_value("15")
    c.syms["INT_RANGE_10_20"].set_value("15")
    verify_value("HEX_RANGE_10_40_DEPENDENT", "0x15")
    verify_value("INT_RANGE_10_40_DEPENDENT", "15")
    c.unset_values()
    verify_range("HEX_RANGE_10_40_DEPENDENT", 0x10, 0x40,  0x10)
    verify_range("INT_RANGE_10_40_DEPENDENT", 10,   40,    10)

    # Ranges and symbols defined in multiple locations

    verify_value("INACTIVE_RANGE", "2")
    verify_value("ACTIVE_RANGE", "1")

    # TODO: test symbol references in some other way?
    # TODO: test selects in some other way?
    # TODO: test implies in some other way?

    #
    # defconfig_filename
    #

    print("Testing defconfig_filename...")

    c = kconfiglib.Config("Kconfiglib/tests/empty")
    verify(c.defconfig_filename is None,
           "defconfig_filename should be None with no defconfig_list symbol")

    c = kconfiglib.Config("Kconfiglib/tests/Kdefconfig_nonexistent")
    verify(c.defconfig_filename is None,
           "defconfig_filename should be None when none of the files in the "
           "defconfig_list symbol exist")

    # Referenced in Kdefconfig_existent(_but_n)
    os.environ["BAR"] = "defconfig_2"

    c = kconfiglib.Config("Kconfiglib/tests/Kdefconfig_existent_but_n")
    verify(c.defconfig_filename is None,
           "defconfig_filename should be None when the condition is n for all "
           "the defaults")

    c = kconfiglib.Config("Kconfiglib/tests/Kdefconfig_existent")
    verify(c.defconfig_filename == "Kconfiglib/tests/defconfig_2",
           "defconfig_filename should return the existent file "
           "Kconfiglib/tests/defconfig_2")

    # Should also look relative to $srctree if the defconfig is an absolute
    # path and not found

    c = kconfiglib.Config("Kconfiglib/tests/Kdefconfig_srctree")
    verify(c.defconfig_filename == "Kconfiglib/tests/defconfig_2",
           "defconfig_filename gave wrong file with $srctree unset")

    os.environ["srctree"] = "Kconfiglib/tests"
    c = kconfiglib.Config("Kconfiglib/tests/Kdefconfig_srctree")
    verify(c.defconfig_filename == "Kconfiglib/tests/sub/defconfig_in_sub",
           "defconfig_filename gave wrong file with $srctree set")

    #
    # mainmenu_text
    #

    print("Testing mainmenu_text...")

    c = kconfiglib.Config("Kconfiglib/tests/empty")
    verify(c.mainmenu_text == "Linux Kernel Configuration",
           "An empty Kconfig should get a default main menu prompt")

    # Expanded in the mainmenu text
    os.environ["FOO"] = "bar baz"
    c = kconfiglib.Config("Kconfiglib/tests/Kmainmenu")
    verify(c.mainmenu_text == "---bar baz---",
           "Wrong mainmenu text")

    #
    # Misc. minor APIs
    #

    os.environ["ENV_VAR"] = "foo"
    # Contains reference to undefined environment variable, so disable warnings
    c = kconfiglib.Config("Kconfiglib/tests/Kmisc", warn = False)

    print("Testing is_optional...")

    verify(not get_choices(c)[0].is_optional,
           "First choice should not be optional")
    verify(get_choices(c)[1].is_optional,
           "Second choice should be optional")

    print("Testing user_value...")

    # Avoid warnings from assigning invalid user values and assigning user
    # values to symbols without prompts
    c.disable_warnings()

    syms = [c.syms[name] for name in \
      ("BOOL", "TRISTATE", "STRING", "INT", "HEX")]

    for sym in syms:
        verify(sym.user_value is None,
               "{} should not have a user value to begin with")

    # Assign valid values for the types

    assign_and_verify_user_value("BOOL", "n", "n")
    assign_and_verify_user_value("BOOL", "y", "y")
    assign_and_verify_user_value("TRISTATE", "n", "n")
    assign_and_verify_user_value("TRISTATE", "m", "m")
    assign_and_verify_user_value("TRISTATE", "y", "y")
    assign_and_verify_user_value("STRING", "foo bar", "foo bar")
    assign_and_verify_user_value("INT", "123", "123")
    assign_and_verify_user_value("HEX", "0x123", "0x123")

    # Assign invalid values for the types. They should retain their old user
    # value.

    assign_and_verify_user_value("BOOL", "m", "y")
    assign_and_verify_user_value("BOOL", "foo", "y")
    assign_and_verify_user_value("BOOL", "1", "y")
    assign_and_verify_user_value("TRISTATE", "foo", "y")
    assign_and_verify_user_value("TRISTATE", "1", "y")
    assign_and_verify_user_value("INT", "foo", "123")
    assign_and_verify_user_value("HEX", "foo", "0x123")

    for s in syms:
        s.unset_value()
        verify(s.user_value is None,
               "{} should not have a user value after being reset".
               format(s.name))

    print("Testing defined vs undefined symbols...")

    for name in "A", "B", "C", "D", "BOOL", "TRISTATE", "STRING", "INT", "HEX":
        verify(c.syms[name].nodes,
               "{} should be defined".format(name))

    for name in "NOT_DEFINED_1", "NOT_DEFINED_2", "NOT_DEFINED_3", \
                "NOT_DEFINED_4":
        sym = c.syms[name]
        verify(not c.syms[name].nodes,
               "{} should not be defined".format(name))

    print("Testing Symbol.choice...")

    for name in "A", "B", "C", "D":
        verify(c.syms[name].choice is not None,
               "{} should be a choice symbol".format(name))

    for name in "Q1", "Q2", "Q3", "BOOL", "TRISTATE", "STRING", "INT", "HEX", \
                "FROM_ENV", "FROM_ENV_MISSING", "NOT_DEFINED_1", \
                "NOT_DEFINED_2", "NOT_DEFINED_3", "NOT_DEFINED_4":
        verify(c.syms[name].choice is None,
               "{} should not be a choice symbol".format(name))

    print("Testing is_allnoconfig_y...")

    verify(not c.syms["NOT_ALLNOCONFIG_Y"].is_allnoconfig_y,
           "NOT_ALLNOCONFIG_Y should not be allnoconfig_y")
    verify(c.syms["ALLNOCONFIG_Y"].is_allnoconfig_y,
           "ALLNOCONFIG_Y should be allnoconfig_y")

    print("Testing UNAME_RELEASE...")

    verify_value("UNAME_RELEASE", platform.uname()[2])
    ur = c.syms["UNAME_RELEASE"]
    verify(ur.config is c and
           ur.type == kconfiglib.STRING and
           ur.env_var == "<uname release>",
           "UNAME_RELEASE has wrong fields")

    #
    # .config reading and writing
    #

    print("Testing .config reading and writing...")

    config_test_file = "Kconfiglib/tests/config_test"

    def write_and_verify_header(header):
        c.write_config(config_test_file, header)
        c.load_config(config_test_file)
        verify(c.config_header == header,
               "The header {} morphed into {} on loading"
               .format(repr(header), repr(c.config_header)))

    def verify_file_contents(fname, contents):
        with open(fname, "r") as f:
            file_contents = f.read()
            verify(file_contents == contents,
                   "{} contains '{}'. Expected '{}'."
                   .format(fname, file_contents, contents))

    # Writing/reading strings with characters that need to be escaped

    c = kconfiglib.Config("Kconfiglib/tests/Kescape")

    # Test the default value
    c.write_config(config_test_file + "_from_def", header="")
    verify_file_contents(config_test_file + "_from_def",
                         r'''CONFIG_STRING="\"\\"''' "\n")
    # Write our own value
    c.syms["STRING"].set_value(r'''\"a'\\''')
    c.write_config(config_test_file + "_from_user", header="")
    verify_file_contents(config_test_file + "_from_user",
                         r'''CONFIG_STRING="\\\"a'\\\\"''' "\n")

    # Read back the two configs and verify the respective values
    c.load_config(config_test_file + "_from_def")
    verify_value("STRING", '"\\')
    c.load_config(config_test_file + "_from_user")
    verify_value("STRING", r'''\"a'\\''')

    # Appending values from a .config

    c = kconfiglib.Config("Kconfiglib/tests/Kappend")

    # Values before assigning
    verify_value("BOOL", "n")
    verify_value("STRING", "")

    # Assign BOOL
    c.load_config("Kconfiglib/tests/config_set_bool", replace = False)
    verify_value("BOOL", "y")
    verify_value("STRING", "")

    # Assign STRING
    c.load_config("Kconfiglib/tests/config_set_string", replace = False)
    verify_value("BOOL", "y")
    verify_value("STRING", "foo bar")

    # Reset BOOL
    c.load_config("Kconfiglib/tests/config_set_string")
    verify_value("BOOL", "n")
    verify_value("STRING", "foo bar")

    # Loading a completely empty .config should reset values
    c.load_config("Kconfiglib/tests/empty")
    verify_value("STRING", "")

    # An indented assignment in a .config should be ignored
    c.load_config("Kconfiglib/tests/config_indented")
    verify_value("IGNOREME", "y")

    #
    # .config
    #

    print("Testing .config...")

    c1 = kconfiglib.Config("Kconfiglib/tests/Kmisc", warn = False)
    c2 = kconfiglib.Config("Kconfiglib/tests/Kmisc", warn = False)

    c1_undef, c1_bool, c1_choice, c1_menu, c1_comment = c1.syms["BOOL"], \
        c1.syms["NOT_DEFINED_1"], get_choices(c1)[0], get_menus(c1)[0], \
        get_comments(c1)[0]
    c2_undef, c2_bool, c2_choice, c2_menu, c2_comment = c2.syms["BOOL"], \
        c2.syms["NOT_DEFINED_1"], get_choices(c2)[0], get_menus(c2)[0], \
        get_comments(c2)[0]

    verify((c1_undef is not c2_undef) and (c1_bool is not c2_bool) and
           (c1_choice is not c2_choice) and (c1_menu is not c2_menu) and
           (c1_comment is not c2_comment) and
           (c1_undef.config   is c1) and (c2_undef.config   is c2) and
           (c1_bool.config    is c1) and (c2_bool.config    is c2) and
           (c1_choice.config  is c1) and (c2_choice.config  is c2) and
           (c1_menu.config    is c1) and (c2_menu.config    is c2) and
           (c1_comment.config is c1) and (c2_comment.config is c2),
           "Config instance state separation or .config is broken")

    #
    # Imply semantics
    #

    print("Testing imply semantics...")

    c = kconfiglib.Config("Kconfiglib/tests/Kimply")

    verify_value("IMPLY_DIRECT_DEPS", "y")
    verify_value("UNMET_DIRECT_1", "n")
    verify_value("UNMET_DIRECT_2", "n")
    verify_value("UNMET_DIRECT_3", "n")
    verify_value("MET_DIRECT_1", "y")
    verify_value("MET_DIRECT_2", "y")
    verify_value("MET_DIRECT_3", "y")
    verify_value("MET_DIRECT_4", "y")

    verify_value("IMPLY_COND", "y")
    verify_value("IMPLIED_N_COND", "n")
    verify_value("IMPLIED_M_COND", "m")
    verify_value("IMPLIED_Y_COND", "y")

    verify_value("IMPLY_N_1", "n")
    verify_value("IMPLY_N_2", "n")
    verify_value("IMPLIED_FROM_N_1", "n")
    verify_value("IMPLIED_FROM_N_2", "n")

    verify_value("IMPLY_M", "m")
    verify_value("IMPLIED_M", "m")
    verify_value("IMPLIED_M_BOOL", "y")

    verify_value("IMPLY_M_TO_Y", "y")
    verify_value("IMPLIED_M_TO_Y", "y")

    # Test user value semantics

    # Verify that IMPLIED_TRISTATE is invalidated if the direct
    # dependencies change

    assign_and_verify("IMPLY", "y")
    assign_and_verify("DIRECT_DEP", "y")
    verify_value("IMPLIED_TRISTATE", "y")
    assign_and_verify("DIRECT_DEP", "n")
    verify_value("IMPLIED_TRISTATE", "n")
    # Set back for later tests
    assign_and_verify("DIRECT_DEP", "y")

    # Verify that IMPLIED_TRISTATE can be set to anything when IMPLY has value
    # "n", and that it gets the value "n" by default (for non-imply-related
    # reasons)

    assign_and_verify("IMPLY", "n")
    assign_and_verify("IMPLIED_TRISTATE", "n")
    assign_and_verify("IMPLIED_TRISTATE", "m")
    assign_and_verify("IMPLIED_TRISTATE", "y")
    c.syms["IMPLIED_TRISTATE"].unset_value()
    verify_value("IMPLIED_TRISTATE", "n")

    # Same as above for "m". Anything still goes, but "m" by default now.

    assign_and_verify("IMPLY", "m")
    assign_and_verify("IMPLIED_TRISTATE", "n")
    assign_and_verify("IMPLIED_TRISTATE", "m")
    assign_and_verify("IMPLIED_TRISTATE", "y")
    c.syms["IMPLIED_TRISTATE"].unset_value()
    verify_value("IMPLIED_TRISTATE", "m")

    # Same as above for "y". Only "n" and "y" should be accepted. "m" gets
    # promoted to "y". Default should be "y".

    assign_and_verify("IMPLY", "y")
    assign_and_verify("IMPLIED_TRISTATE", "n")
    assign_and_verify_value("IMPLIED_TRISTATE", "m", "y")
    assign_and_verify("IMPLIED_TRISTATE", "y")
    c.syms["IMPLIED_TRISTATE"].unset_value()
    verify_value("IMPLIED_TRISTATE", "y")

    # Being implied to either "m" or "y" should give a bool the value "y"

    c.syms["IMPLY"].unset_value()
    verify_value("IMPLIED_BOOL", "n")
    assign_and_verify("IMPLY", "n")
    verify_value("IMPLIED_BOOL", "n")
    assign_and_verify("IMPLY", "m")
    verify_value("IMPLIED_BOOL", "y")
    assign_and_verify("IMPLY", "y")
    verify_value("IMPLIED_BOOL", "y")

    # A bool implied to "m" or "y" can take the values "n" and "y"

    c.syms["IMPLY"].set_value("m")
    assign_and_verify("IMPLIED_BOOL", "n")
    assign_and_verify("IMPLIED_BOOL", "y")

    c.syms["IMPLY"].set_value("y")
    assign_and_verify("IMPLIED_BOOL", "n")
    assign_and_verify("IMPLIED_BOOL", "y")

    #
    # Choice semantics
    #

    print("Testing choice semantics...")

    c = kconfiglib.Config("Kconfiglib/tests/Kchoice")

    choice_bool, choice_bool_opt, choice_tristate, choice_tristate_opt, \
      choice_bool_m, choice_tristate_m, choice_defaults, \
      choice_defaults_not_visible, choice_no_type_bool, \
      choice_no_type_tristate, choice_missing_member_type_1, \
      choice_missing_member_type_2, choice_weird_syms = get_choices(c)

    for choice in (choice_bool, choice_bool_opt, choice_bool_m,
                   choice_defaults):
        verify(choice.type == kconfiglib.BOOL,
               "choice {} should have type bool".format(choice.name))

    # TODO: fix this laters. type automatically changed.
    #for choice in (choice_tristate, choice_tristate_opt, choice_tristate_m):
    #    verify(choice.type == kconfiglib.TRISTATE,
    #           "choice {} should have type tristate"
    #           .format(choice.name))

    def select_and_verify(sym):
        choice = get_parent(sym)
        sym.set_value("y")
        verify(choice.value == "y",
               'The mode of the choice should be "y" after selecting a '
               "symbol")
        verify(sym.choice.selection is sym,
               "{} should be the selected choice symbol"
               .format(sym.name))
        verify(choice.selection is sym,
               "{} should be the selected symbol".format(sym.name))
        verify(choice.user_selection is sym,
               "{} should be the user selection of the choice"
               .format(sym.name))

    def select_and_verify_all(choice):
        # Select in forward order
        for sym in choice.syms:
            select_and_verify(sym)
        # Select in reverse order
        for i in range(len(choice.syms) - 1, 0, -1):
            select_and_verify(choice.syms[i])

    def verify_mode(choice, no_modules_mode, modules_mode):
        c.syms["MODULES"].set_value("n")
        choice_mode = choice.value
        verify(choice_mode == no_modules_mode,
               'Wrong mode for choice {} with no modules. Expected "{}", '
               'got "{}".'.format(choice.name, no_modules_mode, choice_mode))

        c.syms["MODULES"].set_value("y")
        choice_mode = choice.value
        verify(choice_mode == modules_mode,
               'Wrong mode for choice {} with modules. Expected "{}", '
               'got "{}".'.format(choice.name, modules_mode,
                                   choice_mode))

    verify_mode(choice_bool, "y", "y")
    verify_mode(choice_bool_opt, "n", "n")
    verify_mode(choice_tristate, "y", "m")
    verify_mode(choice_tristate_opt, "n", "n")
    verify_mode(choice_bool_m, "y", "y")
    verify_mode(choice_tristate_m, "y", "m")

    # Test defaults

    c.syms["TRISTATE_SYM"].set_value("n")
    verify(choice_defaults.selection is c.syms["OPT_4"],
           "Wrong choice default with TRISTATE_SYM = n")
    c.syms["TRISTATE_SYM"].set_value("y")
    verify(choice_defaults.selection is c.syms["OPT_2"],
           "Wrong choice default with TRISTATE_SYM = y")
    c.syms["OPT_1"].set_value("y")
    verify(choice_defaults.selection is c.syms["OPT_1"],
           "User selection should override defaults")

    verify(choice_defaults_not_visible.selection is c.syms["OPT_8"],
           "Non-visible choice symbols should cause the next default to be "
           "considered")

    # Test "y" mode selection

    c.syms["MODULES"].set_value("y")

    select_and_verify_all(choice_bool)
    select_and_verify_all(choice_bool_opt)
    select_and_verify_all(choice_tristate)
    select_and_verify_all(choice_tristate_opt)
    # For BOOL_M, the mode should have been promoted
    select_and_verify_all(choice_bool_m)

    # Test "m" mode selection...

    # ...for a choice that can also be in "y" mode

    for sym_name in ("T_1", "T_2"):
        assign_and_verify_value(sym_name, "m", "m")
        verify(choice_tristate.value == "m",
               'Selecting {} to "m" should have changed the mode of the '
               'choice to "m"'.format(sym_name))

        assign_and_verify_value(sym_name, "y", "y")
        verify(choice_tristate.value == "y" and
               choice_tristate.selection is c.syms[sym_name],
               'Selecting {} to "y" should have changed the mode of the '
               'choice to "y" and made it the selection'.format(sym_name))

    # ...for a choice that can only be in "m" mode

    for sym_name in ("TM_1", "TM_2"):
        assign_and_verify_value(sym_name, "m", "m")
        assign_and_verify_value(sym_name, "n", "n")
        # "y" should be truncated
        assign_and_verify_value(sym_name, "y", "m")
        verify(choice_tristate_m.value == "m",
               'A choice that can only be in "m" mode was not')

    # Verify that choices with no explicitly specified type get the type of the
    # first contained symbol with a type

    verify(choice_no_type_bool.type == kconfiglib.BOOL,
           "Expected first choice without explicit type to have type bool")
    verify(choice_no_type_tristate.type == kconfiglib.TRISTATE,
           "Expected second choice without explicit type to have type "
           "tristate")

    # Verify that symbols without a type in the choice get the type of the
    # choice

    verify((c.syms["MMT_1"]._type, c.syms["MMT_2"]._type,
            c.syms["MMT_3"]._type) ==
             (kconfiglib.BOOL, kconfiglib.BOOL, kconfiglib.TRISTATE),
           "Wrong types for first choice with missing member types")

    verify((c.syms["MMT_4"]._type, c.syms["MMT_5"]._type) ==
             (kconfiglib.BOOL, kconfiglib.BOOL),
           "Wrong types for second choice with missing member types")

    # Verify that symbols in choices that depend on the preceding symbol aren't
    # considered choice symbols

    def verify_is_normal_choice_symbol(name):
        sym = c.syms[name]
        verify(sym.choice is not None and
               sym in choice_weird_syms.syms and
               get_parent(sym) is choice_weird_syms,
               "{} should be a normal choice symbol".format(sym.name))

    # TODO: parent stuff

    def verify_is_weird_choice_symbol(name):
        sym = c.syms[name]
        verify(sym.choice is None and
               sym not in choice_weird_syms.syms,
               "{} should be a weird (non-)choice symbol"
               .format(sym.name))

    verify_is_normal_choice_symbol("WS1")
    verify_is_weird_choice_symbol("WS2")
    verify_is_weird_choice_symbol("WS3")
    verify_is_weird_choice_symbol("WS4")
    verify_is_weird_choice_symbol("WS5")
    verify_is_normal_choice_symbol("WS6")
    verify_is_weird_choice_symbol("WS7")
    verify_is_weird_choice_symbol("WS8")
    verify_is_normal_choice_symbol("WS9")

    #
    # Object dependencies
    #

    print("Testing object dependencies...")

    # Note: This tests an internal API

    c = kconfiglib.Config("Kconfiglib/tests/Kdep")

    def verify_dependent(sym_name, deps_names):
        sym = c.syms[sym_name]
        deps = [c.syms[name] for name in deps_names]
        sym_deps = sym._get_dependent()
        verify(len(sym_deps) == len(set(sym_deps)),
               "{}'s dependencies contains duplicates".format(sym_name))
        sym_deps = [item for item in sym_deps
                         if not isinstance(item, kconfiglib.Choice)]
        verify(len(sym_deps) == len(deps),
               "Wrong number of dependent symbols for {}".format(sym_name))
        for dep in deps:
            verify(dep in sym_deps, "{} should depend on {}".
                                    format(dep.name, sym_name))

    # Test twice to cover dependency caching
    for i in range(0, 2):
        n_deps = 39
        # Verify that D1, D2, .., D<n_deps> are dependent on D
        verify_dependent("D", ("D{}".format(i) for i in range(1, n_deps + 1)))
        # Choices
        verify_dependent("A", ("B", "C"))
        verify_dependent("B", ("A", "C"))
        verify_dependent("C", ("A", "B"))
        verify_dependent("S", ("A", "B", "C"))

    # Verify that the last symbol depends on the first in a long chain of
    # dependencies. Test twice to cover dependency caching.

    c = kconfiglib.Config("Kconfiglib/tests/Kchain")

    for i in range(0, 2):
        verify(c.syms["CHAIN_26"] in c.syms["CHAIN_1"]._get_dependent(),
               "Dependency chain broken")

    print("Testing compatibility with weird selects/implies...")

    # Check that Kconfiglib doesn't crash for stuff like 'select n' (seen in
    # U-Boot). These probably originate from misunderstandings of how Kconfig
    # works.
    kconfiglib.Config("Kconfiglib/tests/Kwtf")

    print("\nAll selftests passed\n" if all_passed else
          "\nSome selftests failed\n")

def run_compatibility_tests():
    """Runs tests on configurations from the kernel. Tests compability with the
    C implementation by comparing outputs."""

    os.environ.pop("ARCH", None)
    os.environ.pop("SRCARCH", None)
    os.environ.pop("srctree", None)

    if speedy and not os.path.exists("scripts/kconfig/conf"):
        print("\nscripts/kconfig/conf does not exist -- running "
              "'make allnoconfig' to build it...")
        shell("make allnoconfig")

    print("Running compatibility tests...\n")

    # The set of tests that want to run for all architectures in the kernel
    # tree -- currently, all tests. The boolean flag indicates whether .config
    # (generated by the C implementation) should be compared to ._config
    # (generated by us) after each invocation.
    all_arch_tests = [(test_load,           False),
                      (test_config_absent,  True),
                      (test_call_all,       False),
                      (test_all_no,         True),
                      (test_all_yes,        True),
                      (test_all_no_simpler, True),
                      # Needs to report success/failure for each arch/defconfig
                      # combo, hence False.
                      (test_defconfig,      False)]

    arch_srcarch_list = get_arch_srcarch_list()

    for test_fn, compare_configs in all_arch_tests:
        # The test description is taken from the docstring of the corresponding
        # function
        print(textwrap.dedent(test_fn.__doc__))

        for arch, srcarch in arch_srcarch_list:
            rm_configs()

            os.environ["ARCH"] = arch
            os.environ["SRCARCH"] = srcarch
            # Previously we used to load all the arches once and keep them
            # around for the tests. That now uses a huge amount of memory (pypy
            # helps a bit), so reload them for each test instead.
            test_fn(kconfiglib.Config(), arch)

            # Let kbuild infer SRCARCH from ARCH if we aren't in speedy mode.
            # This could detect issues with the test suite.
            if not speedy:
                del os.environ["SRCARCH"]

            if compare_configs:
                if equal_confs():
                    print("  {:14}OK".format(arch))
                else:
                    print("  {:14}FAIL".format(arch))
                    fail()

    if all_passed:
        print("All selftests and compatibility tests passed")
        print(nconfigs, "arch/defconfig pairs tested")
    else:
        print("Some tests failed")

def get_arch_srcarch_list():
    """Returns a list of (ARCH, SRCARCH) tuples to test."""

    res = []

    def add_arch(arch):
        res.append((arch, srcarch))

    for srcarch in os.listdir("arch"):
        if os.path.exists(os.path.join("arch", srcarch, "Kconfig")):
            add_arch(srcarch)
            # Some arches define additional ARCH settings with ARCH != SRCARCH
            # (search for "Additional ARCH settings for" in the Makefile)
            if srcarch == "x86":
                add_arch("i386")
                add_arch("x86_64")
            elif srcarch == "sparc":
                add_arch("sparc32")
                add_arch("sparc64")
            elif srcarch == "sh":
                add_arch("sh64")
            elif srcarch == "tile":
                add_arch("tilepro")
                add_arch("tilegx")

    return res

def test_load(conf, arch):
   """Load all arch Kconfigs to make sure we don't throw any errors"""
   print("  {:14}OK".format(arch))

# The weird docstring formatting is to get the format right when we print the
# docstring ourselves
def test_all_no(conf, arch):
    """
    Verify that our examples/allnoconfig.py script generates the same .config
    as 'make allnoconfig', for each architecture. Runs the script via
    'make scriptconfig', so kinda slow even in speedy mode."""

    # TODO: Support speedy mode for running the script
    shell("make scriptconfig SCRIPT=Kconfiglib/examples/allnoconfig.py "
          "PYTHONCMD='{}'".format(sys.executable))
    shell("mv .config ._config")
    if speedy:
        shell("scripts/kconfig/conf --allnoconfig Kconfig")
    else:
        shell("make allnoconfig")

def test_all_no_simpler(conf, arch):
    """
    Verify that our examples/allnoconfig_simpler.py script generates the same
    .config as 'make allnoconfig', for each architecture. Runs the script via
    'make scriptconfig', so kinda slow even in speedy mode."""

    # TODO: Support speedy mode for running the script
    shell("make scriptconfig SCRIPT=Kconfiglib/examples/allnoconfig_simpler.py "
          "PYTHONCMD='{}'".format(sys.executable))
    shell("mv .config ._config")
    if speedy:
        shell("scripts/kconfig/conf --allnoconfig Kconfig")
    else:
        shell("make allnoconfig")

def test_all_yes(conf, arch):
    """
    Verify that our examples/allyesconfig.py script generates the same .config
    as 'make allyesconfig', for each architecture. Runs the script via
    'make scriptconfig', so kinda slow even in speedy mode."""

    # TODO: Support speedy mode for running the script
    shell("make scriptconfig SCRIPT=Kconfiglib/examples/allyesconfig.py "
          "PYTHONCMD='{}'".format(sys.executable))
    shell("mv .config ._config")
    if speedy:
        shell("scripts/kconfig/conf --allyesconfig Kconfig")
    else:
        shell("make allyesconfig")

def test_call_all(conf, arch):
    """
    Call all public methods on all symbols, menus, choices, and comments for
    all architectures to make sure we never crash or hang. (Nearly all public
    methods: some are hard to test like this, but are exercised by other
    tests.)"""
    print("  For {}...".format(arch))

    conf.defconfig_filename
    conf.mainmenu_text
    conf.enable_undef_warnings()
    conf.disable_undef_warnings()
    conf.disable_warnings()
    conf.enable_warnings()
    conf.unset_values()

    # Python 2/3 compatible
    for _, s in conf.syms.items():
        s.__str__()
        s.__repr__()
        s.assignable
        s.type
        s.value
        s.visibility
        s.unset_value()

    # Cheat with internals
    for c in conf._choices:
        c.__str__()
        c.__repr__()
        c.value
	c.assignable
        c.selection
	c.default_selection
        c.type
        c.visibility

def test_config_absent(conf, arch):
    """
    Verify that Kconfiglib generates the same .config as 'make alldefconfig',
    for each architecture"""
    conf.write_config("._config")
    if speedy:
        shell("scripts/kconfig/conf --alldefconfig Kconfig")
    else:
        shell("make alldefconfig")

def test_defconfig(conf, arch):
    """
    Verify that Kconfiglib generates the same .config as scripts/kconfig/conf,
    for each architecture/defconfig pair. In obsessive mode, this test includes
    nonsensical groupings of arches with defconfigs from other arches (every
    arch/defconfig combination) and takes an order of magnitude longer time to
    run.

    With logging enabled, this test appends any failures to a file
    test_defconfig_fails in the root."""

    global nconfigs
    defconfigs = []

    def add_configs_for_arch(arch_):
        arch_dir = os.path.join("arch", arch_)
        # Some arches have a "defconfig" in the root of their arch/<arch>/
        # directory
        root_defconfig = os.path.join(arch_dir, "defconfig")
        if os.path.exists(root_defconfig):
            defconfigs.append(root_defconfig)
        # Assume all files in the arch/<arch>/configs directory (if it
        # exists) are configurations
        defconfigs_dir = os.path.join(arch_dir, "configs")
        if not os.path.exists(defconfigs_dir):
            return
        if not os.path.isdir(defconfigs_dir):
            print("Warning: '{}' is not a directory - skipping"
                  .format(defconfigs_dir))
            return
        for dirpath, _, filenames in os.walk(defconfigs_dir):
            for filename in filenames:
                defconfigs.append(os.path.join(dirpath, filename))

    if obsessive:
        # Collect all defconfigs. This could be done once instead, but it's
        # a speedy operation comparatively.
        for arch_ in os.listdir("arch"):
            add_configs_for_arch(arch_)
    else:
        add_configs_for_arch(arch)

    # Test architecture for each defconfig

    for defconfig in defconfigs:
        rm_configs()

        nconfigs += 1

        conf.load_config(defconfig)
        conf.write_config("._config")
        if speedy:
            shell("scripts/kconfig/conf --defconfig='{}' Kconfig".
                  format(defconfig))
        else:
            shell("cp {} .config".format(defconfig))
            # It would be a bit neater if we could use 'make *_defconfig'
            # here (for example, 'make i386_defconfig' loads
            # arch/x86/configs/i386_defconfig' if ARCH = x86/i386/x86_64),
            # but that wouldn't let us test nonsensical combinations of
            # arches and defconfigs, which is a nice way to find obscure
            # bugs.
            shell("make kconfiglibtestconfig")

        arch_defconfig_str = "  {:14}with {:60} ".format(arch, defconfig)

        if equal_confs():
            print(arch_defconfig_str + "OK")
        else:
            print(arch_defconfig_str + "FAIL")
            fail()
            if log:
                with open("test_defconfig_fails", "a") as fail_log:
                    fail_log.write("{}  {} with {} did not match\n"
                            .format(time.strftime("%d %b %Y %H:%M:%S",
                                                  time.localtime()),
                                    arch, defconfig))

#
# Helper functions
#

def rm_configs():
    """Delete any old ".config" (generated by the C implementation) and
    "._config" (generated by us), if present."""
    def rm_if_exists(f):
        if os.path.exists(f):
            os.remove(f)

    rm_if_exists(".config")
    rm_if_exists("._config")

def equal_confs():
    with open(".config") as f:
        their = f.readlines()

    # Strip the header generated by 'conf'
    i = 0
    for line in their:
        if not line.startswith("#") or \
           re.match(r"# CONFIG_(\w+) is not set", line):
            break
        i += 1
    their = their[i:]

    try:
        f = open("._config")
    except IOError as e:
        if e.errno != errno.ENOENT:
            raise
        print("._config not found. Did you forget to apply the Makefile patch?")
        return False
    else:
        with f:
            # [1:] strips the default header
            our = f.readlines()[1:]

    if their == our:
        return True

    # Print a unified diff to help debugging
    print("Mismatched .config's! Unified diff:")
    sys.stdout.writelines(difflib.unified_diff(their, our, fromfile="their",
                                               tofile="our"))

    return False

if __name__ == "__main__":
    run_tests()
