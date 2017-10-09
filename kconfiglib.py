"""
Kconfiglib is a Python 2/3 library for scripting and extracting information
from Kconfig-based configuration systems. Features include the following:

 - Programmatic getting and setting of symbol values

 - Reading/writing of .config files

 - Expression inspection and evaluation. All expressions are exposed and use a
   simple format that can be processed manually if needed.

 - Menu tree inspection. The underlying menu tree is exposed, including
   submenus created implicitly by symbols depending on preceding symbols. This
   can be used e.g. to implement menuconfig-like functionality.

 - Highly compatible with the standard Kconfig C tools: The test suite compares
   outputs between Kconfiglib and the C tools on real-world kernel Kconfig and
   defconfig files for a large number of cases (by diffing generated .configs).

 - Pretty speedy by pure Python standards: Parses the x86 Kconfigs in about a
   second on a Core i7 2600K (with a warm file cache). For long-running jobs,
   PyPy gives a nice speedup.

For the Linux kernel, a handy interface is provided by the
scripts/kconfig/Makefile patch. For experimentation, you can use the
iscriptconfig target, which gives an interactive Python prompt where the
configuration for ARCH has been loaded:

  $ make [ARCH=<arch>] iscriptconfig

To run a script, use the scriptconfig target:

  $ make [ARCH=<arch>] scriptconfig SCRIPT=<path to script> [SCRIPT_ARG=<arg>]

See the examples/ subdirectory for example scripts.


The Makefile patch is used to pick up the ARCH, SRCARCH, and KERNELVERSION
environment variables (and any future environment variables that might get
used). If you want to run Kconfiglib without the Makefile patch, the following
will probably work in practice (it's what the test suite does in 'speedy' mode,
except it tests all ARCHes):

  $ ARCH=x86 SRCARCH=x86 KERNELVERSION=`make kernelversion` python script.py

ARCH and SRCARCH (the arch/ subdirectory) might differ in some cases. Search
for "Additional ARCH settings for" in the top-level Makefile to see the
possible variations. The value of KERNELVERSION doesn't seem to matter as of
Linux 4.14.

Kconfiglib will warn if you forget to set some environment variable that's
referenced in the configuration (via 'option env="ENV_VAR"').


When using scriptconfig, scripts receive the name of the Kconfig file to load
in sys.argv[1]. As far as I can tell, this is always "Kconfig" from the kernel
top-level directory as of Linux 4.14. If an argument is provided with
SCRIPT_ARG, it appears as sys.argv[2].


Kconfiglib supports both Python 2 and Python 3 (and PyPy). For (i)scriptconfig,
the Python interpreter to use can be passed in PYTHONCMD, which defaults to
"python".


Send bug reports, suggestions, and questions to ulfalizer a.t Google's email
service (or open a ticket on the GitHub page).
"""

import errno
import os
import platform
import re
import sys

# File layout:
#
# Public classes
# Public functions
# Internal classes
# Internal functions
# Public global constants
# Internal global constants

# Line length: 79 columns

#
# Public classes
#

class Config(object):
    """
    Represents a Kconfig configuration, e.g. for x86 or ARM. This is the set of
    symbols, choices, and menu nodes appearing in the configuration. Creating
    any number of Config objects (including for different architectures) is
    safe. Kconfiglib doesn't keep any global state.

    The following attributes are available on Config instances. They should be
    viewed as read-only, and some are implemented through @property magic.
    Modifying symbols is fine, but not the 'syms' dictionary itself.

    syms:
      A dictionary with all symbols in the configuration. The key is the name
      of the symbol, so that e.g. conf.syms["MODULES"] returns the MODULES
      symbol. Symbols that are referenced in expressions but never defined are
      included as well.

    defined_syms:
      A list of all defined symbols, in the same order as they appear in the
      Kconfig files. Provided as a convenience (and also used internally). The
      defined symbols are those whose 'nodes' attribute is non-empty.

    named_choices:
      A dictionary like 'syms' for named choices (choice FOO). This is mostly
      for completeness. I've never seen named choices being used.

    top_menu:
      The menu node (see the MenuNode class) of the top-level menu. Acts as the
      root of the menu tree.

    mainmenu_text:
      The prompt (title) of the top_menu menu, with Kconfig variable references
      ("$FOO") expanded. Defaults to "Linux Kernel Configuration" (like in the
      C tools). Can be changed with the 'mainmenu' statement (see
      kconfig-language.txt).

    defconfig_filename:
      The filename given by the 'option defconfig_list' symbol. This is the
      first existing file with a satisfied condition among the 'default'
      properties of the symbol. If a file is not found at the given path, it is
      also looked up relative to $srctree if set ($srctree/foo/defconfig is
      looked up if foo/defconfig is not found).

      Has the value None if either no defconfig_list symbol exists, or if it
      has no 'default' with a satisfied dependency that points to an existing
      file.

      References to Kconfig symbols ("$FOO") are expanded in 'default'
      properties.

      Setting 'option defconfig_list' on multiple symbols ignores symbols past
      the first one.

      Do print(c.syms["DEFCONFIG_LIST"]) on a kernel configuration to see an
      example of a defconfig_list symbol.

      Something to look out for is that scripts/kconfig/Makefile might use the
      --defconfig=<defconfig> option when calling the C tools of e.g. 'make
      defconfig'. This option overrides the 'option defconfig_list' symbol,
      meaning defconfig_filename might not match what 'make defconfig' would
      use.

    srctree:
      The value of the $srctree environment variable when the configuration was
      loaded, or None if $srctree wasn't set. Kconfig and .config files are
      looked up relative to $srctree if they are not found in the base path
      (unless absolute paths are specified). This is to support out-of-tree
      builds. The C tools use this variable in the same way.

      Changing $srctree after loading the configuration has no effect. Only the
      value when the configuration is loaded matters. This avoids surprises if
      multiple configurations are loaded with different values for $srctree.

    config_prefix:
      The value of the $CONFIG_ environment variable when the configuration was
      loaded. This is the prefix used (and expected) in .config files. Defaults
      to "CONFIG_". Used in the same way in the C tools.

      Like for srctree, only the value of $CONFIG_ when the configuration is
      loaded matters.
    """

    __slots__ = (
        "_choices",
        "_print_undef_assign",
        "_print_warnings",
        "_set_re",
        "_unset_re",
        "config_prefix",
        "defconfig_list",
        "defined_syms",
        "modules",
        "named_choices",
        "srctree",
        "syms",
        "top_menu",
    )

    #
    # Public interface
    #

    def __init__(self, filename="Kconfig", warn=True):
        """
        Creates a new Config object by parsing Kconfig files. Raises
        KconfigSyntaxError on syntax errors. Note that Kconfig files are not
        the same as .config files (which store configuration symbol values).

        filename (default: "Kconfig"):
          The base Kconfig file. For the Linux kernel, you'll want "Kconfig"
          from the top-level directory, as environment variables will make sure
          the right Kconfig is included from there
          (arch/<architecture>/Kconfig). If you are using Kconfiglib via 'make
          scriptconfig', the filename of the base base Kconfig file will be in
          sys.argv[1] (always "Kconfig" in practice).

          The $srctree environment variable is used if set (see the class
          documentation).

        warn (default: True):
          True if warnings related to this configuration should be printed to
          stderr. This can be changed later with
          Config.enable/disable_warnings(). It is provided as a constructor
          argument since warnings might be generated during parsing.
        """

        self.syms = {}
        self.defined_syms = []
        self.named_choices = {}

        # Used for quickly invalidating all choices
        self._choices = []

        # Predefined symbol. DEFCONFIG_LIST has been seen using this.
        uname_sym = Symbol()
        uname_sym._type = STRING
        uname_sym.name = "UNAME_RELEASE"
        uname_sym.config = self
        uname_sym.defaults.append((platform.uname()[2], None))
        # env_var doubles as the SYMBOL_AUTO flag from the C implementation, so
        # just set it to something. The naming breaks a bit here, but it's
        # pretty obscure.
        uname_sym.env_var = "<uname release>"
        self.syms["UNAME_RELEASE"] = uname_sym

        # The symbol with "option defconfig_list" set, containing a list of
        # default .config files
        self.defconfig_list = None

        self.config_prefix = os.environ.get("CONFIG_")
        if self.config_prefix is None:
            self.config_prefix = "CONFIG_"

        # Regular expressions for parsing .config files
        self._set_re = re.compile(r"{}(\w+)=(.*)"
                                  .format(self.config_prefix))
        self._unset_re = re.compile(r"# {}(\w+) is not set"
                                    .format(self.config_prefix))

        self.srctree = os.environ.get("srctree")

        self._print_warnings = warn
        self._print_undef_assign = False

        self.top_menu = MenuNode()
        self.top_menu.config = self
        self.top_menu.item = MENU
        self.top_menu.visibility = None
        self.top_menu.prompt = ("Linux Kernel Configuration", None)
        self.top_menu.parent = None
        self.top_menu.dep = None
        self.top_menu.filename = filename
        self.top_menu.linenr = 1

        # We hardcode MODULES for backwards compatibility. Proper support via
        # 'option modules' wouldn't be that tricky to add with backwards
        # compatibility either though.
        self.modules = self._lookup_sym("MODULES")

        # Parse the Kconfig files
        self._parse_block(_FileFeed(self._open(filename), filename),
                          None, self.top_menu, None, None, self.top_menu)

        self.top_menu.list = self.top_menu.next
        self.top_menu.next = None

        _finalize_tree(self.top_menu)

        # Build Symbol._direct_dependents for all symbols
        self._build_dep()

    @property
    def mainmenu_text(self):
        """
        See the class documentation.
        """
        return self._expand_sym_refs(self.top_menu.prompt[0])

    @property
    def defconfig_filename(self):
        """
        See the class documentation.
        """
        if self.defconfig_list is None:
            return None
        for filename, cond_expr in self.defconfig_list.defaults:
            if eval_expr(cond_expr) != "n":
                filename = self._expand_sym_refs(filename)
                try:
                    with self._open(filename) as f:
                        return f.name
                except IOError:
                    continue

        return None

    def load_config(self, filename, replace=True):
        """
        Loads symbol values from a file in the .config format. Equivalent to
        calling Symbol.set_value() to set each of the values.

        "# CONFIG_FOO is not set" within a .config file is treated specially
        and sets the user value of FOO to 'n'. The C tools work the same way.

        filename:
          The .config file to load. The $srctree variable is used if set (see
          the class documentation).

        replace (default: True): True if all existing user values should
          be cleared before loading the .config.
        """

        with self._open(filename) as f:
            if replace:
                # Invalidates all symbols as a side effect
                self.unset_values()
            else:
                self._invalidate_all()

            # Small optimizations
            set_re_match = self._set_re.match
            unset_re_match = self._unset_re.match
            syms = self.syms

            for linenr, line in enumerate(f, 1):
                # The C tools ignore trailing whitespace
                line = line.rstrip()

                set_match = set_re_match(line)
                if set_match:
                    name, val = set_match.groups()
                    if name not in syms:
                        self._warn_undef_assign_load(name, val, filename,
                                                     linenr)
                        continue

                    sym = syms[name]

                    if sym._type == STRING and val.startswith('"'):
                        if len(val) < 2 or val[-1] != '"':
                            self._warn("malformed string literal", filename,
                                       linenr)
                            continue
                        # Strip quotes and remove escapings. The unescaping
                        # procedure should be safe since " can only appear as
                        # \" inside the string.
                        val = val[1:-1].replace('\\"', '"') \
                                       .replace("\\\\", "\\")

                    if sym.choice is not None:
                        mode = sym.choice.user_value
                        if mode is not None and mode != val:
                            self._warn("assignment to {} changes mode of "
                                       'containing choice from "{}" to "{}".'
                                       .format(name, val, mode),
                                       filename, linenr)

                else:
                    unset_match = unset_re_match(line)
                    if not unset_match:
                        continue

                    name = unset_match.group(1)
                    if name not in syms:
                        self._warn_undef_assign_load(name, "n", filename,
                                                     linenr)
                        continue

                    sym = syms[name]
                    val = "n"

                # Done parsing the assignment. Set the value.

                if sym.user_value is not None:
                    self._warn('{} set more than once. Old value: "{}", new '
                               'value: "{}".'
                               .format(name, sym.user_value, val),
                               filename, linenr)

                sym._set_value_no_invalidate(val, True)

    def write_config(self, filename,
                     header="# Generated by Kconfiglib (https://github.com/ulfalizer/Kconfiglib)\n"):
        """
        Writes out symbol values in .config format.

        Kconfiglib makes sure the format matches what the C tools would
        generate, down to whitespace. This eases testing.

        filename: The filename under which to save the configuration.

        header (default: "# Generated by Kconfiglib (https://github.com/ulfalizer/Kconfiglib)\n"):
            Text that will be inserted verbatim at the beginning of the file.
            You would usually want each line to start with '#' to make it a
            comment, and include a final terminating newline.
        """
        with open(filename, "w") as f:
            f.write(header)
            f.writelines(self._get_config_strings())

    def eval_string(self, s):
        """
        Returns the value of the expression 's', represented as a string, in
        the context of the configuration. Raises KconfigSyntaxError if syntax
        errors are detected in 's'.

        As an example, if FOO and BAR are tristate symbols at least one of
        which has the value "y", then config.eval_string("y && (FOO || BAR)")
        returns "y".

        This function always yields a tristate value. To get the value of
        non-bool, non-tristate symbols, use Symbol.value.

        The result of this function is consistent with how evaluation works for
        conditional ('if ...') expressions in the configuration (as well as in
        the C tools). m is rewritten to 'm && MODULES'.
        """
        return eval_expr(self._parse_expr(self._tokenize(s, True),
                                          s,
                                          None,   # filename
                                          None,   # linenr
                                          True))  # transform_m

    def unset_values(self):
        """
        Resets the user values of all symbols, as if Config.load_config() or
        Symbol.set_value() had never been called.
        """

        # set_value() already rejects undefined symbols, and they don't
        # need to be invalidated (because their value never changes), so we can
        # just iterate over defined symbols.

        for sym in self.defined_syms:
            # We're iterating over all symbols, so no need for symbols to
            # invalidate their dependent symbols
            sym.user_value = None
            sym._invalidate()

        for choice in self._choices:
            choice.user_value = choice.user_selection = None
            choice._invalidate()

    def enable_warnings(self):
        """
        See Config.__init__().
        """
        self._print_warnings = True

    def disable_warnings(self):
        """
        See Config.__init__().
        """
        self._print_warnings = False

    def enable_undef_warnings(self):
        """
        Enables printing of warnings to stderr for assignments to undefined
        symbols. Disabled by default since it tends to be spammy for Kernel
        configurations (and mostly suggests cleanups).
        """
        self._print_undef_assign = True

    def disable_undef_warnings(self):
        """
        See enable_undef_assign().
        """
        self._print_undef_assign = False

    def __repr__(self):
        """
        Prints some general information when a Config object is evaluated.
        """
        fields = (
            "configuration with {} symbols".format(len(self.syms)),
            'main menu prompt "{}"'.format(self.mainmenu_text),
            "srctree not set" if self.srctree is None else
                'srctree "{}"'.format(self.srctree),
            'config symbol prefix "{}"'.format(self.config_prefix),
            "warnings " + ("enabled" if self._print_warnings else "disabled"),
            "undef. symbol assignment warnings " +
                ("enabled" if self._print_undef_assign else "disabled")
        )

        return "<{}>".format(", ".join(fields))

    #
    # Private methods
    #

    #
    # File reading
    #

    def _open(self, filename):
        """
        First tries to open 'filename', then '$srctree/filename' if $srctree
        was set when the configuration was loaded.
        """
        try:
            return open(filename)
        except IOError as e:
            if not os.path.isabs(filename) and self.srctree is not None:
                filename = os.path.join(self.srctree, filename)
                try:
                    return open(filename)
                except IOError as e2:
                    # This is needed for Python 3, because e2 is deleted after
                    # the try block:
                    #
                    # https://docs.python.org/3/reference/compound_stmts.html#the-try-statement
                    e = e2

            raise IOError(
                'Could not open "{}" ({}: {}). Perhaps the $srctree '
                "environment variable (which was {}) is set incorrectly. Note "
                "that the current value of $srctree is saved when the Config "
                "instance is created (for consistency and to cleanly "
                "separate instances)."
                .format(filename, errno.errorcode[e.errno], e.strerror,
                        "unset" if self.srctree is None else
                        '"{}"'.format(self.srctree)))

    #
    # Kconfig parsing
    #

    def _tokenize(self, s, for_eval, filename=None, linenr=None):
        """
        Returns a _Feed instance containing tokens derived from the string 's'.
        Registers any new symbols encountered (via _lookup_sym()).

        Tries to be reasonably speedy by processing chunks of text via regexes
        and string operations where possible. This is a hotspot during parsing.

        for_eval:
          True when parsing an expression for a call to Config.eval_string(),
          in which case we should not treat the first token specially nor
          register new symbols.
        """

        # Tricky implementation detail: While parsing a token, 'token' refers
        # to the previous token. See _NOT_REF for why this is needed.

        if for_eval:
            token = None
            tokens = []

            # The current index in the string being tokenized
            i = 0

        else:
            # See comment at _initial_token_re_match definition
            initial_token_match = _initial_token_re_match(s)
            if not initial_token_match:
                return None

            keyword = _get_keyword(initial_token_match.group(1))
            if keyword == _T_HELP:
                # Avoid junk after "help", e.g. "---", being registered as a
                # symbol
                return _Feed((_T_HELP,))
            if keyword is None:
                # We expect a keyword as the first token
                _tokenization_error(s, filename, linenr)

            token = keyword
            tokens = [keyword]
            # The current index in the string being tokenized
            i = initial_token_match.end()

        # Main tokenization loop (for tokens past the first one)
        while i < len(s):
            # Test for an identifier/keyword first. This is the most common
            # case.
            id_keyword_match = _id_keyword_re_match(s, i)
            if id_keyword_match:
                # We have an identifier or keyword

                # Jump past it
                i = id_keyword_match.end()

                # Check what it is. lookup_sym() will take care of allocating
                # new symbols for us the first time we see them. Note that
                # 'token' still refers to the previous token.

                name = id_keyword_match.group(1)
                keyword = _get_keyword(name)
                if keyword is not None:
                    # It's a keyword
                    token = keyword

                elif token not in _STRING_LEX:
                    # It's a symbol
                    if name in ("n", "m", "y"):
                        # Always represent n, m, y as strings (constant
                        # symbols). This simplifies the expression logic.
                        token = name
                    else:
                        token = self._lookup_sym(name, for_eval)

                else:
                    # It's a case of missing quotes. For example, the
                    # following is accepted:
                    #
                    #   menu unquoted_title
                    #
                    #   config A
                    #       tristate unquoted_prompt
                    #
                    #   endmenu
                    token = name

            else:
                # Not an identifier/keyword

                # Note: _id_keyword_match and _initial_token_match strip
                # trailing whitespace, making it safe to assume s[i] is the
                # start of a token here. We manually strip trailing whitespace
                # below as well.
                #
                # An old version stripped whitespace in this spot instead, but
                # that leads to some redundancy and would cause
                # _id_keyword_match to be tried against just "\n" fairly often
                # (because file.readlines() keeps newlines).

                c = s[i]
                i += 1

                if c in "\"'":
                    # String literal/constant symbol
                    if "\\" not in s:
                        # Fast path: If the string contains no backslashes, we
                        # can just find the matching quote.
                        end = s.find(c, i)
                        if end == -1:
                            _tokenization_error(s, filename, linenr)
                        token = s[i:end]
                        i = end + 1
                    else:
                        # Slow path: This could probably be sped up, but it's a
                        # very unusual case anyway.
                        quote = c
                        val = ""
                        while 1:
                            if i >= len(s):
                                _tokenization_error(s, filename, linenr)
                            c = s[i]
                            if c == quote:
                                break
                            if c == "\\":
                                if i + 1 >= len(s):
                                    _tokenization_error(s, filename, linenr)
                                val += s[i + 1]
                                i += 2
                            else:
                                val += c
                                i += 1
                        i += 1
                        token = val

                elif c == "&":
                    # Invalid characters are ignored
                    if i >= len(s) or s[i] != "&": continue
                    token = _T_AND
                    i += 1

                elif c == "|":
                    # Invalid characters are ignored
                    if i >= len(s) or s[i] != "|": continue
                    token = _T_OR
                    i += 1

                elif c == "!":
                    if i < len(s) and s[i] == "=":
                        token = _T_UNEQUAL
                        i += 1
                    else:
                        token = _T_NOT

                elif c == "=":
                    token = _T_EQUAL

                elif c == "(":
                    token = _T_OPEN_PAREN

                elif c == ")":
                    token = _T_CLOSE_PAREN

                elif c == "#": break # Comment

                # Very rare
                elif c == "<":
                    if i < len(s) and s[i] == "=":
                        token = _T_LESS_EQUAL
                        i += 1
                    else:
                        token = _T_LESS

                # Very rare
                elif c == ">":
                    if i < len(s) and s[i] == "=":
                        token = _T_GREATER_EQUAL
                        i += 1
                    else:
                        token = _T_GREATER

                else:
                    # Invalid characters are ignored
                    continue

                # Skip trailing whitespace
                while i < len(s) and s[i].isspace():
                    i += 1

            tokens.append(token)

        return _Feed(tokens)

    def _parse_block(self, line_feeder, end_marker, parent, visible_if_deps,
                     prev_line, prev_node):
        """
        Parses a block, which is the contents of either a file or an if, menu,
        or choice statement.

        line_feeder:
          A _FileFeed instance feeding lines from a file. The Kconfig language
          is line-based in practice.

        end_marker:
          The token that ends the block, e.g. _T_ENDIF ("endif") for ifs. None
          for files.

        parent:
          The parent menu node, corresponding to e.g. a menu or Choice. Can
          also be a Symbol, due to automatic submenu creation from
          dependencies.

        visible_if_deps:
          'visible if' dependencies from enclosing menus. Propagated to Symbol
          and Choice prompts.

        prev_line:
          A "cached" (line, tokens) tuple from having parsed a line earlier
          that we realized belonged to a different construct.

        prev_node:
          The previous menu node. New nodes will be added after this one (by
          modifying its 'next' pointer).

          Through a trick, prev_node is also used to parse a list of children
          (for a menu or Choice): After parsing the children, the 'next'
          pointer is assigned to the 'list' pointer to "tilt up" the children
          above the node.


        Returns the final menu node in the block (or prev_node if the block is
        empty). This allows for easy chaining.
        """

        while 1:
            if prev_line is not None:
                line, tokens = prev_line
            else:
                line = line_feeder.next()
                if line is None:
                    if end_marker is not None:
                        raise KconfigSyntaxError("Unexpected end of file " +
                                                 line_feeder.filename)

                    # We have reached the end of the file. Terminate the final
                    # node and return it.
                    prev_node.next = None
                    return prev_node

                tokens = self._tokenize(line, False, line_feeder.filename,
                                        line_feeder.linenr)
                if tokens is None:
                    continue

            t0 = tokens.next()

            # Cases are ordered roughly by frequency, which speeds things up a
            # bit

            if t0 in (_T_CONFIG, _T_MENUCONFIG):
                # The tokenizer will automatically allocate a new Symbol object
                # for any new names it encounters, so we don't need to worry
                # about that here.
                sym = tokens.next()

                node = MenuNode()
                node.config = self
                node.item = sym
                node.help = None
                node.list = None
                node.parent = parent
                node.filename = line_feeder.filename
                node.linenr = line_feeder.linenr
                node.is_menuconfig = (t0 == _T_MENUCONFIG)

                prev_line = self._parse_properties(line_feeder, node,
                                                   visible_if_deps)

                sym.nodes.append(node)
                self.defined_syms.append(sym)

                # Tricky Python semantics: This assign prev_node.next before
                # prev_node
                prev_node.next = prev_node = node

            elif t0 == _T_SOURCE:
                kconfig_file = tokens.next()
                exp_kconfig_file = self._expand_sym_refs(kconfig_file)

                try:
                    f = self._open(exp_kconfig_file)
                except IOError as e:
                    # Extend the error message a bit in this case
                    raise IOError(
                        "{}:{}: {} Also note that e.g. $FOO in a 'source' "
                        "statement does not refer to the environment "
                        "variable FOO, but rather to the Kconfig Symbol FOO "
                        "(which would commonly have 'option env=\"FOO\"' in "
                        "its definition)."
                        .format(line_feeder.filename, line_feeder.linenr,
                                e.message))

                prev_node = self._parse_block(_FileFeed(f, exp_kconfig_file),
                                              None, parent, visible_if_deps,
                                              None, prev_node)
                prev_line = None

            elif t0 == end_marker:
                # We have reached the end of the block. Terminate the final
                # node and return it.
                prev_node.next = None
                return prev_node

            elif t0 == _T_IF:
                node = MenuNode()
                node.item = None
                node.prompt = None
                node.parent = parent
                node.filename = line_feeder.filename
                node.linenr = line_feeder.linenr
                node.dep = \
                    _make_and(parent.dep,
                              self._parse_expr(tokens, line,
                                               line_feeder.filename,
                                               line_feeder.linenr, True))

                self._parse_block(line_feeder, _T_ENDIF, node, visible_if_deps,
                                  None, node)
                node.list = node.next

                prev_line = None

                prev_node.next = prev_node = node

            elif t0 == _T_MENU:
                node = MenuNode()
                node.config = self
                node.item = MENU
                node.visibility = None
                node.parent = parent
                node.filename = line_feeder.filename
                node.linenr = line_feeder.linenr

                prev_line = self._parse_properties(line_feeder, node,
                                                   visible_if_deps)
                node.prompt = (tokens.next(), node.dep)

                self._parse_block(line_feeder, _T_ENDMENU, node,
                                  _make_and(visible_if_deps, node.visibility),
                                  prev_line, node)
                node.list = node.next

                prev_line = None

                prev_node.next = prev_node = node

            elif t0 == _T_COMMENT:
                node = MenuNode()
                node.config = self
                node.item = COMMENT
                node.list = None
                node.parent = parent
                node.filename = line_feeder.filename
                node.linenr = line_feeder.linenr

                prev_line = self._parse_properties(line_feeder, node,
                                                   visible_if_deps)
                node.prompt = (tokens.next(), node.dep)

                prev_node.next = prev_node = node

            elif t0 == _T_CHOICE:
                name = tokens.next()
                if name is None:
                    choice = Choice()
                    self._choices.append(choice)
                else:
                    # Named choice
                    choice = self.named_choices.get(name)
                    if choice is None:
                        choice = Choice()
                        self._choices.append(choice)
                        choice.name = name
                        self.named_choices[name] = choice

                choice.config = self

                node = MenuNode()
                node.config = self
                node.item = choice
                node.help = None
                node.parent = parent
                node.filename = line_feeder.filename
                node.linenr = line_feeder.linenr

                prev_line = self._parse_properties(line_feeder, node,
                                                   visible_if_deps)
                self._parse_block(line_feeder, _T_ENDCHOICE, node,
                                  visible_if_deps, prev_line, node)
                node.list = node.next

                prev_line = None

                choice.nodes.append(node)

                prev_node.next = prev_node = node

            elif t0 == _T_MAINMENU:
                self.top_menu.prompt = (tokens.next(), None)
                self.top_menu.filename = line_feeder.filename
                self.top_menu.linenr = line_feeder.linenr

            else:
                _parse_error(line, "unrecognized construct",
                             line_feeder.filename, line_feeder.linenr)

    def _parse_cond(self, tokens, line, filename, linenr):
        """
        Parses an optional 'if <expr>' construct and returns the parsed <expr>,
        or None if the next token is not _T_IF
        """
        return self._parse_expr(tokens, line, filename, linenr, True) \
               if tokens.check(_T_IF) else None

    def _parse_val_and_cond(self, tokens, line, filename, linenr):
        """
        Parses '<expr1> if <expr2>' constructs, where the 'if' part is
        optional. Returns a tuple containing the parsed expressions, with None
        as the second element if the 'if' part is missing.
        """
        return (self._parse_expr(tokens, line, filename, linenr, False),
                self._parse_cond(tokens, line, filename, linenr))

    def _parse_properties(self, line_feeder, node, visible_if_deps):
        """
        Parses properties for symbols, menus, choices, and comments. Also takes
        care of propagating dependencies from the menu node to the properties
        of the item (this mirrors the inner working of the C tools).

        line_feeder:
          A _FileFeed instance feeding lines from a file. The Kconfig language
          is line-based in practice.

        node:
          The menu node we're parsing properties on. Some properties (prompts,
          help texts, 'depends on') apply to the Menu node, while the others
          apply to the contained item.

        visible_if_deps:
          'visible if' dependencies from enclosing menus. Propagated to Symbol
          and Choice prompts.

        Stops when finding a line that isn't part of the properties, and
        returns a (line, tokens) tuple for it so it can be reused.
        """

        # New properties encountered at this location. A local 'depends on'
        # only applies to these, in case a symbol is defined in multiple
        # locations.
        prompt = None
        defaults = []
        selects = []
        implies = []
        ranges = []

        # Menu node dependency from 'depends on'. Will get propagated to the
        # properties above.
        node.dep = None

        # The cached (line, tokens) tuple that we return
        last_line = None

        while 1:
            line = line_feeder.next()
            if line is None:
                break

            filename = line_feeder.filename
            linenr = line_feeder.linenr

            tokens = self._tokenize(line, False, filename, linenr)
            if tokens is None:
                continue

            t0 = tokens.next()

            if t0 == _T_DEPENDS:
                if not tokens.check(_T_ON):
                    _parse_error(line, 'expected "on" after "depends"',
                                 filename, linenr)

                node.dep = \
                    _make_and(node.dep,
                              self._parse_expr(tokens, line, filename,
                                               linenr, True))

            elif t0 == _T_HELP:
                # Find first non-blank (not all-space) line and get its
                # indentation

                while 1:
                    line = line_feeder.next_no_join()
                    if line is None or not line.isspace():
                        break

                if line is None:
                    node.help = ""
                    break

                indent = _indentation(line)
                if indent == 0:
                    # If the first non-empty lines has zero indent, there is no
                    # help text
                    node.help = ""
                    line_feeder.linenr -= 1
                    break

                # The help text goes on till the first non-empty line with less
                # indent

                help_lines = [_deindent(line, indent).rstrip()]
                while 1:
                    line = line_feeder.next_no_join()
                    if line is None or \
                       (not line.isspace() and _indentation(line) < indent):
                        node.help = "\n".join(help_lines).rstrip() + "\n"
                        break
                    help_lines.append(_deindent(line, indent).rstrip())

                if line is None:
                    break

                line_feeder.linenr -= 1

            elif t0 == _T_SELECT:
                if not isinstance(node.item, Symbol):
                    _parse_error(line, "only symbols can select", filename,
                                 linenr)

                # HACK: We always represent n/m/y using the constant symbol
                # "n"/"m"/"y" forms, but that causes a crash if a Kconfig file
                # does e.g. 'select n' (which is meaningless and probably stems
                # from a misunderstanding). Seen in U-Boot. Just skip the
                # select.
                target = tokens.next()
                if target not in ("n", "m", "y"):
                    selects.append(
                        (target,
                         self._parse_cond(tokens, line, filename, linenr)))

            elif t0 == _T_IMPLY:
                if not isinstance(node.item, Symbol):
                    _parse_error(line, "only symbols can imply", filename,
                                 linenr)

                # See above
                target = tokens.next()
                if target not in ("n", "m", "y"):
                    implies.append(
                        (target,
                         self._parse_cond(tokens, line, filename, linenr)))

            elif t0 in (_T_BOOL, _T_TRISTATE, _T_INT, _T_HEX, _T_STRING):
                node.item._type = _TOKEN_TO_TYPE[t0]
                if tokens.peek() is not None:
                    prompt = self._parse_val_and_cond(tokens, line,
                                                      filename, linenr)

            elif t0 == _T_DEFAULT:
                defaults.append(
                    self._parse_val_and_cond(tokens, line, filename, linenr))

            elif t0 in (_T_DEF_BOOL, _T_DEF_TRISTATE):
                node.item._type = _TOKEN_TO_TYPE[t0]
                if tokens.peek() is not None:
                    defaults.append(
                        self._parse_val_and_cond(tokens, line, filename,
                                                 linenr))

            elif t0 == _T_PROMPT:
                # 'prompt' properties override each other within a single
                # definition of a symbol, but additional prompts can be added
                # by defining the symbol multiple times
                prompt = self._parse_val_and_cond(tokens, line, filename,
                                                  linenr)

            elif t0 == _T_RANGE:
                ranges.append(
                    (tokens.next(),
                     tokens.next(),
                     self._parse_cond(tokens, line, filename, linenr)))

            elif t0 == _T_OPTION:
                if tokens.check(_T_ENV) and tokens.check(_T_EQUAL):
                    env_var = tokens.next()

                    node.item.env_var = env_var

                    if env_var not in os.environ:
                        self._warn("the symbol {0} references the "
                                   "non-existent environment variable {1} "
                                   "(meaning the 'option env=\"{1}\"' will "
                                   "have no effect). If you're using "
                                   "Kconfiglib via 'make (i)scriptconfig', it "
                                   "should have set up the environment "
                                   "correctly for you. If you still got this "
                                   "message, that might be an error, and you "
                                   "should email ulfalizer a.t Google's email "
                                   "service.".format(node.item.name, env_var),
                                   filename, linenr)
                    else:
                        defaults.append((os.environ[env_var], None))

                elif tokens.check(_T_DEFCONFIG_LIST):
                    if self.defconfig_list is None:
                        self.defconfig_list = node.item
                    else:
                        self._warn("'option defconfig_list' set on multiple "
                                   "symbols ({0} and {1}). Only {0} will be "
                                   "used."
                                   .format(self.defconfig_list.name,
                                           node.item.name))

                elif tokens.check(_T_MODULES):
                    # To reduce warning spam, only warn if 'option modules' is
                    # set on some symbol that isn't MODULES, which should be
                    # safe. I haven't run into any projects that make use
                    # modules besides the kernel yet, and there it's likely to
                    # keep being called "MODULES".
                    if node.item is not self.modules:
                        self._warn("the 'modules' option is not supported. "
                                   "Let me know if this is a problem for you; "
                                   "it shouldn't be that hard to implement. "
                                   "(Note that modules are still supported -- "
                                   "Kconfiglib just assumes the symbol name "
                                   "MODULES, like older versions of the C "
                                   "implementation did when 'option modules' "
                                   "wasn't used.)",
                                   filename, linenr)

                elif tokens.check(_T_ALLNOCONFIG_Y):
                    if not isinstance(node.item, Symbol):
                        _parse_error(line,
                                     "the 'allnoconfig_y' option is only "
                                     "valid for symbols",
                                     filename, linenr)

                    node.item.is_allnoconfig_y = True

                else:
                    _parse_error(line, "unrecognized option", filename, linenr)

            elif t0 == _T_VISIBLE:
                if not tokens.check(_T_IF):
                    _parse_error(line, 'expected "if" after "visible"',
                                 filename, linenr)

                node.visibility = \
                    _make_and(node.visibility,
                              self._parse_expr(tokens, line, filename, linenr,
                                               True))

            elif t0 == _T_OPTIONAL:
                if not isinstance(node.item, Choice):
                    _parse_error(line,
                                 '"optional" is only valid for choices',
                                 filename,
                                 linenr)

                node.item.is_optional = True

            else:
                tokens.i = 0
                last_line = (line, tokens)
                break

        # Done parsing properties. Now add the new
        # prompts/defaults/selects/implies/ranges properties, with dependencies
        # from node.dep propagated.

        # First propagate parent dependencies to node.dep
        node.dep = _make_and(node.dep, node.parent.dep)

        if isinstance(node.item, (Symbol, Choice)):
            if isinstance(node.item, Symbol):
                node.item.direct_deps = \
                    _make_or(node.item.direct_deps, node.dep)

            # Set the prompt, with dependencies propagated
            if prompt is not None:
                node.prompt = (prompt[0],
                               _make_and(_make_and(prompt[1], node.dep),
                                         visible_if_deps))
            else:
                node.prompt = None

            # Add the new defaults, with dependencies propagated
            for val_expr, cond_expr in defaults:
                node.item.defaults.append(
                    (val_expr, _make_and(cond_expr, node.dep)))

            # Add the new ranges, with dependencies propagated
            for low, high, cond_expr in ranges:
                node.item.ranges.append(
                    (low, high, _make_and(cond_expr, node.dep)))

            # Handle selects
            for target, cond_expr in selects:
                # Only stored for convenience. Not used during evaluation.
                node.item.selects.append(
                    (target, _make_and(cond_expr, node.dep)))

                # Modify the dependencies of the selected symbol
                target.rev_dep = \
                    _make_or(target.rev_dep,
                             _make_and(node.item,
                                       _make_and(cond_expr, node.dep)))

            # Handle implies
            for target, cond_expr in implies:
                # Only stored for convenience. Not used during evaluation.
                node.item.implies.append(
                    (target, _make_and(cond_expr, node.dep)))

                # Modify the dependencies of the implied symbol
                target.weak_rev_dep = \
                    _make_or(target.weak_rev_dep,
                             _make_and(node.item,
                                       _make_and(cond_expr, node.dep)))

        # Return cached non-property line
        return last_line

    def _parse_expr(self, feed, line, filename, linenr, transform_m):
        """
        Parses an expression from the tokens in 'feed' using a simple top-down
        approach. The result has the form
        '(<operator> <operand 1> <operand 2>)' where <operator> is e.g.
        kconfiglib._AND. If there is only one operand (i.e., no && or ||), then
        the operand is returned directly. This also goes for subexpressions.

        As an example, A && B && (!C || D == 3) is represented as the tuple
        structure (_AND, A, (_AND, B, (_OR, (_NOT, C), (_EQUAL, D, 3)))), with
        the Symbol objects stored directly in the expression.

        feed:
          _Feed instance containing the tokens for the expression.

        line:
          The line containing the expression being parsed.

        filename:
          The file containing the expression. None when using
          Config.eval_string().

        linenr:
          The line number containing the expression. None when using
          Config.eval_string().

        transform_m:
          True if 'm' should be rewritten to 'm && MODULES'. See
          the Config.eval_string() documentation.
        """

        # Grammar:
        #
        #   expr:     and_expr ['||' expr]
        #   and_expr: factor ['&&' and_expr]
        #   factor:   <symbol> ['='/'!='/'<'/... <symbol>]
        #             '!' factor
        #             '(' expr ')'
        #
        # It helps to think of the 'expr: and_expr' case as a single-operand OR
        # (no ||), and of the 'and_expr: factor' case as a single-operand AND
        # (no &&). Parsing code is always a bit tricky.

        # Mind dump: parse_factor() and two nested loops for OR and AND would
        # work as well. The straightforward implementation there gives a
        # (op, (op, (op, A, B), C), D) parse for A op B op C op D. Representing
        # expressions as (op, [list of operands]) instead goes nicely with that
        # version, but is wasteful for short expressions and complicates
        # expression evaluation and other code that works on expressions (more
        # complicated code likely offsets any performance gain from less
        # recursion too). If we also try to optimize the list representation by
        # merging lists when possible (e.g. when ANDing two AND expressions),
        # we end up allocating a ton of lists instead of reusing expressions,
        # which is bad.

        and_expr = self._parse_and_expr(feed, line, filename, linenr,
                                        transform_m)

        # Return 'and_expr' directly if we have a "single-operand" OR.
        # Otherwise, parse the expression on the right and make an _OR node.
        # This turns A || B || C || D into
        # (_OR, A, (_OR, B, (_OR, C, D))).
        return and_expr \
               if not feed.check(_T_OR) else \
               (_OR, and_expr, self._parse_expr(feed, line, filename, linenr,
                                                transform_m))

    def _parse_and_expr(self, feed, line, filename, linenr, transform_m):
        factor = self._parse_factor(feed, line, filename, linenr, transform_m)

        # Return 'factor' directly if we have a "single-operand" AND.
        # Otherwise, parse the right operand and make an _AND node. This turns
        # A && B && C && D into (_AND, A, (_AND, B, (_AND, C, D))).
        return factor \
               if not feed.check(_T_AND) else \
               (_AND, factor, self._parse_and_expr(feed, line, filename,
                                                   linenr, transform_m))

    def _parse_factor(self, feed, line, filename, linenr, transform_m):
        token = feed.next()

        if isinstance(token, (Symbol, str)):
            # Plain symbol or relation

            next_token = feed.peek()
            if next_token not in _TOKEN_TO_REL:
                # Plain symbol

                # For conditional expressions ('depends on <expr>',
                # '... if <expr>', etc.), "m" and m are rewritten to
                # "m" && MODULES.
                if transform_m and token == "m":
                    return (_AND, "m", self.modules)

                return token

            # Relation
            return (_TOKEN_TO_REL[feed.next()], token, feed.next())

        if token == _T_NOT:
            return (_NOT, self._parse_factor(feed, line, filename, linenr,
                                             transform_m))

        if token == _T_OPEN_PAREN:
            expr_parse = self._parse_expr(feed, line, filename,
                                          linenr, transform_m)
            if not feed.check(_T_CLOSE_PAREN):
                _parse_error(line, "missing end parenthesis", filename, linenr)
            return expr_parse

        _parse_error(line, "malformed expression", filename, linenr)

    #
    # Symbol lookup
    #

    def _lookup_sym(self, name, for_eval=False):
        """
        Fetches the symbol 'name' from the symbol table, creating and
        registering it if it does not exist. If 'for_eval' is True, the symbol
        won't be added to the symbol table if it does not exist. This is for
        Config.eval_string().
        """
        if name in self.syms:
            return self.syms[name]

        sym = Symbol()
        sym.config = self
        sym.name = name
        if for_eval:
            self._warn("no symbol {} in configuration".format(name))
        else:
            self.syms[name] = sym
        return sym

    #
    # .config generation
    #

    def _get_config_strings(self):
        """
        Returns a list containing all .config strings for the configuration.
        """

        config_strings = []
        add_fn = config_strings.append

        node = self.top_menu.list
        if node is None:
            # Empty configuration
            return config_strings

        # Symbol._already_written is set to True when a symbol config string is
        # fetched, so that symbols defined in multiple locations only get one
        # .config entry. We reset it prior to writing out a new .config. It
        # only needs to be reset for defined symbols, because undefined symbols
        # will never be written out (because they do not appear structure
        # rooted at Config.top_menu).
        #
        # The C tools reuse _write_to_conf for this, but we cache
        # _write_to_conf together with the value and don't invalidate cached
        # values when writing .config files, so that won't work.
        for sym in self.defined_syms:
            sym._already_written = False

        while 1:
            if isinstance(node.item, Symbol):
                sym = node.item
                if not sym._already_written:
                    config_string = sym.config_string
                    if config_string is not None:
                        add_fn(config_string)
                    sym._already_written = True

            elif (node.item == MENU and eval_expr(node.dep) != "n" and
                  eval_expr(node.visibility) != "n") or \
                 (node.item == COMMENT and eval_expr(node.dep) != "n"):
                add_fn("\n#\n# {}\n#\n".format(node.prompt[0]))

            # Iterative tree walk using parent pointers

            if node.list is not None:
                node = node.list
            elif node.next is not None:
                node = node.next
            else:
                while node.parent is not None:
                    node = node.parent
                    if node.next is not None:
                        node = node.next
                        break
                else:
                    return config_strings

    #
    # Dependency tracking (for caching and invalidation)
    #

    def _build_dep(self):
        """
        Populates the Symbol._direct_dependents sets, linking the symbol to the
        symbols that immediately depend on it in the sense that changing the
        value of the symbol might affect the values of those other symbols.
        This is used for caching/invalidation purposes. The calculated sets
        might be larger than necessary as we don't do any complicated analysis
        of the expressions.
        """

        # Adds 'sym' as a directly dependent symbol to all symbols that appear
        # in the expression 'expr'
        def add_expr_deps(expr, sym):
            res = []
            _expr_syms(expr, res)
            for expr_sym in res:
                expr_sym._direct_dependents.add(sym)

        # The directly dependent symbols of a symbol S are:
        #
        #  - Any symbols whose prompts, default values, rev_dep (select
        #    condition), weak_rev_dep (imply condition), or ranges depend on S
        #
        #  - Any symbol that has S as a direct dependency (has S in
        #    direct_deps). This is needed to get invalidation right for
        #    'imply'.
        #
        #  - Any symbols that belong to the same choice statement as S
        #    (these won't be included in S._direct_dependents as that makes the
        #    dependency graph unwieldy, but S._get_dependent() will include
        #    them)
        #
        #  - Any symbols in a choice statement that depends on S

        # Only calculate _direct_dependents for defined symbols. Undefined
        # symbols could theoretically be selected/implied, but it wouldn't
        # change their value (they always evaluate to their name), so it's not
        # a true dependency.

        for sym in self.defined_syms:
            for node in sym.nodes:
                if node.prompt is not None:
                    add_expr_deps(node.prompt[1], sym)

            for value, cond in sym.defaults:
                add_expr_deps(value, sym)
                add_expr_deps(cond, sym)

            add_expr_deps(sym.rev_dep, sym)
            add_expr_deps(sym.weak_rev_dep, sym)

            for l, u, e in sym.ranges:
                add_expr_deps(l, sym)
                add_expr_deps(u, sym)
                add_expr_deps(e, sym)

            add_expr_deps(sym.direct_deps, sym)

            if sym.choice is not None:
                for node in sym.choice.nodes:
                    if node.prompt is not None:
                        add_expr_deps(node.prompt[1], sym)
                for _, e in sym.choice.defaults:
                    add_expr_deps(e, sym)

    def _invalidate_all(self):
        # Undefined symbols never change value and don't need to be
        # invalidated, so we can just iterate over defined symbols
        for sym in self.defined_syms:
            sym._invalidate()

        for choice in self._choices:
            choice._invalidate()

    #
    # Printing and misc.
    #

    def _expand_sym_refs(self, s):
        """
        Expands $-references to symbols in 's' to symbol values, or to the
        empty string for undefined symbols.
        """

        while 1:
            sym_ref_match = _sym_ref_re_search(s)
            if sym_ref_match is None:
                return s

            sym = self.syms.get(sym_ref_match.group(1))

            s = s[:sym_ref_match.start()] + \
                (sym.value if sym is not None else "") + \
                s[sym_ref_match.end():]

    #
    # Warnings
    #

    def _warn(self, msg, filename=None, linenr=None):
        """For printing general warnings."""
        if self._print_warnings:
            _stderr_msg("warning: " + msg, filename, linenr)

    def _warn_undef_assign(self, msg, filename=None, linenr=None):
        """
        See the class documentation.
        """
        if self._print_undef_assign:
            _stderr_msg("warning: " + msg, filename, linenr)

    def _warn_undef_assign_load(self, name, val, filename, linenr):
        """
        Special version for load_config().
        """
        self._warn_undef_assign(
            'attempt to assign the value "{}" to the undefined symbol {}' \
            .format(val, name), filename, linenr)

class Symbol(object):
    """
    Represents a configuration symbol:

      (menu)config FOO
          ...

    The following attributes are available on Symbol instances. They should be
    viewed as read-only, and some are implemented through @property magic (but
    are still efficient to access due to internal caching).

    (Note: Prompts and help texts are stored in the Symbol's MenuNode(s) rather
    than the Symbol itself. This matches the C tools.)

    name:
      The name of the symbol, e.g. "FOO" for 'config FOO'.

    type:
      The type of the symbol. One of BOOL, TRISTATE, STRING, INT, HEX, UNKNOWN.
      UNKNOWN is for undefined symbols and symbols defined without a type.

      When running without modules (CONFIG_MODULES=n), TRISTATE symbols
      magically change type to BOOL. This also happens for symbols within
      choices in "y" mode. This matches the C tools, and makes sense for
      menuconfig-like functionality. (Check the implementation of the property
      if you need to get the original type.)

    value:
      The current value of the symbol. Automatically recalculated as
      dependencies change.

    assignable:
       A string containing the tristate values that can be assigned to the
       symbol, ordered from lowest (n) to highest (y). This corresponds to the
       selections available in the 'menuconfig' interface. The assignable
       values are calculated from the Symbol's visibility and selects/implies.

       Returns the empty string for non-BOOL/TRISTATE and symbols with
       visibility "n". The other possible values are "ny", "nmy", "my", "m",
       and "y". A "m" or "y" result means the symbol is visible but "locked" to
       that particular value (through a select, perhaps in combination with a
       prompt dependency). menuconfig seems to represent this as -M- and -*-,
       respectively.

       Some handy 'assignable' idioms:

         # Is the symbol assignable (visible)?
         if sym.assignable:
             # What's the highest value it can be assigned? [-1] in Python
             # gives the last element.
             sym_high = sym.assignable[-1]

             # The lowest?
             sym_low = sym.assignable[0]

         # Can the symbol be assigned the value "m"?
         if "m" in sym.assignable:
             ...

    visibility:
      The visibility of the symbol's prompt(s): one of "n", "m", or "y". This
      acts as an upper bound on the values the user can set for the symbol (via
      Symbol.set_value() or a .config file). User values higher than the
      visibility are truncated down to the visibility.

      If the visibility is "n", the user value is ignored, and the symbol is
      not visible in e.g. the menuconfig interface. The visibility of symbols
      without prompts is always "n". Symbols with "n" visibility can only get a
      non-"n" value through a default, select, or imply.

      Note that 'depends on' and parent dependencies (including 'visible if'
      dependencies) are propagated to the prompt dependencies. Additional
      dependencies can be specified with e.g. 'bool "foo" if <cond>".

    user_value:
      The value assigned with Symbol.set_value(), or None if no value has been
      assigned. This won't necessarily match 'value' even if set, as
      dependencies and prompt visibility take precedence.

      Note that you should use Symbol.set_value() to change this value.
      Properties are always read-only.

    config_string:
      The .config assignment string that would get written out for the symbol
      by Config.write_config(). None if no .config assignment would get written
      out. In general, visible symbols, symbols with (active) defaults, and
      selected symbols get written out.

    nodes:
      A list of MenuNode's for this symbol. For most symbols, this list will
      contain a single MenuNode. Undefined symbols get an empty list, and
      symbols defined in multiple locations get one node for each location.

    choice:
      Holds the parent Choice for choice symbols, and None for non-choice
      symbols. Doubles as a flag for whether a symbol is a choice symbol.

    defaults:
      List of (default, cond) tuples for the symbol's 'default's. For example,
      'default A && B if C || D' is represented as ((AND, A, B), (OR, C, D)).
      If there is no condition, 'cond' is None.

      Note that 'depends on' and parent dependencies are propagated to
      'default' conditions.

    selects:
      List of (symbol, cond) tuples for the symbol's 'select's. For example,
      'select A if B' is represented as (A, B). If there is no condition,
      'cond' is None.

      Note that 'depends on' and parent dependencies are propagated to 'select'
      conditions.

    implies:
      List of (symbol, cond) tuples for the symbol's 'imply's. For example,
      'imply A if B' is represented as (A, B). If there is no condition, 'cond'
      is None.

      Note that 'depends on' and parent dependencies are propagated to 'imply'
      conditions.

    ranges:
      List of (low, high, cond) tuples for the symbol's 'range's. For example,
      'range 1 2 if A' is represented as (1, 2, A). If there is no condition,
      'cond' is None.

      Note that 'depends on' and parent dependencies are propagated to 'range'
      conditions.

      Gotcha: Integers are represented as Symbols too. Undefined symbols get
      their name as their value, so this works out. The C tools work the same
      way.

    rev_dep:
      Reverse dependency expression from being 'select'ed by other symbols.
      Multiple selections get ORed together. A condition on a select is ANDed
      with the selecting symbol. For example, if A has 'select FOO' and B has
      'select FOO if C', then FOO's rev_dep will be '(OR, A, (AND, B, C))'.

    weak_rev_dep:
      Like rev_dep, for imply.

    direct_deps:
      The 'depends on' dependencies. If a symbol is defined in multiple
      locations, the dependencies at each location are ORed together.

    env_var:
      If the Symbol is set from the environment via 'option env="FOO"', this
      contains the name ("FOO") of the environment variable. None for symbols
      that aren't set from the environment.

      Internally, this is only used to print the symbol. The value of the
      environment variable is looked up once when the configuration is parsed.

    is_allnoconfig_y:
      True if the symbol has 'option allnoconfig_y' set on it. This has no
      effect internally, but can be checked by scripts.

    config:
      The Config instance this symbol is from.
    """

    __slots__ = (
        "_already_written",
        "_cached_assignable",
        "_cached_deps",
        "_cached_val",
        "_cached_vis",
        "_direct_dependents",
        "_type",
        "_write_to_conf",
        "choice",
        "config",
        "defaults",
        "direct_deps",
        "env_var",
        "implies",
        "is_allnoconfig_y",
        "name",
        "nodes",
        "ranges",
        "rev_dep",
        "selects",
        "user_value",
        "weak_rev_dep",
    )

    #
    # Public interface
    #

    @property
    def type(self):
        """
        See the class documentation.
        """

        if self._type == TRISTATE and \
           ((self.choice is not None and self.choice.value == "y") or
            self.config.modules.value == "n"):
            return BOOL
        return self._type

    @property
    def value(self):
        """
        See the class documentation.
        """

        if self._cached_val is not None:
            return self._cached_val

        # As a quirk of Kconfig, undefined symbols get their name as their
        # value. This is why things like "FOO = bar" work for seeing if FOO has
        # the value "bar".
        if self._type == UNKNOWN:
            self._cached_val = self.name
            return self.name

        # This will hold the value at the end of the function
        val = _DEFAULT_VALUE[self._type]

        vis = self.visibility

        if self._type in (BOOL, TRISTATE):
            if self.choice is None:
                self._write_to_conf = (vis != "n")

                if vis != "n" and self.user_value is not None:
                    # If the symbol is visible and has a user value, we use
                    # that
                    val = _eval_min(self.user_value, vis)

                else:
                    # Otherwise, we look at defaults and weak reverse
                    # dependencies (implies)

                    for default, cond in self.defaults:
                        cond_val = eval_expr(cond)
                        if cond_val != "n":
                            self._write_to_conf = True
                            val = _eval_min(default, cond_val)
                            break

                    # Weak reverse dependencies are only considered if our
                    # direct dependencies are met
                    if eval_expr(self.direct_deps) != "n":
                        weak_rev_dep_val = \
                            eval_expr(self.weak_rev_dep)
                        if weak_rev_dep_val != "n":
                            self._write_to_conf = True
                            val = _eval_max(val, weak_rev_dep_val)

                # Reverse (select-related) dependencies take precedence
                rev_dep_val = eval_expr(self.rev_dep)
                if rev_dep_val != "n":
                    self._write_to_conf = True
                    val = _eval_max(val, rev_dep_val)

            else:
                # (bool/tristate) symbol in choice. See _get_visibility() for
                # more choice-related logic.

                # Initially
                self._write_to_conf = False

                if vis != "n":
                    mode = self.choice.value

                    if mode != "n":
                        self._write_to_conf = True

                        if mode == "y":
                            val = "y" if self.choice.selection is self else "n"
                        elif self.user_value in ("m", "y"):
                            # mode == "m" and self.user_value is not None or
                            # "n"
                            val = "m"

            # "m" is promoted to "y" in two circumstances:
            #  1) If our type is boolean
            #  2) If our weak_rev_dep (from IMPLY) is "y"
            if val == "m" and \
               (self.type == BOOL or eval_expr(self.weak_rev_dep) == "y"):
                val = "y"

        elif self._type in (INT, HEX):
            base = _TYPE_TO_BASE[self._type]

            # Check if a range is in effect
            for low_expr, high_expr, cond_expr in self.ranges:
                if eval_expr(cond_expr) != "n":
                    has_active_range = True

                    low_str = _str_val(low_expr)
                    high_str = _str_val(high_expr)

                    low = int(low_str, base) if \
                      _is_base_n(low_str, base) else 0
                    high = int(high_str, base) if \
                      _is_base_n(high_str, base) else 0

                    break
            else:
                has_active_range = False

            self._write_to_conf = (vis != "n")

            if vis != "n" and self.user_value is not None and \
               _is_base_n(self.user_value, base) and \
               (not has_active_range or
                low <= int(self.user_value, base) <= high):

                # If the user value is well-formed and satisfies range
                # contraints, it is stored in exactly the same form as
                # specified in the assignment (with or without "0x", etc.)
                val = self.user_value

            else:
                # No user value or invalid user value. Look at defaults.

                for val_expr, cond_expr in self.defaults:
                    if eval_expr(cond_expr) != "n":
                        self._write_to_conf = True

                        # Similarly to above, well-formed defaults are
                        # preserved as is. Defaults that do not satisfy a range
                        # constraints are clamped and take on a standard form.

                        val = _str_val(val_expr)

                        if _is_base_n(val, base):
                            val_num = int(val, base)
                            if has_active_range:
                                clamped_val = None

                                if val_num < low:
                                    clamped_val = low
                                elif val_num > high:
                                    clamped_val = high

                                if clamped_val is not None:
                                    val = (hex(clamped_val)
                                           if self._type == HEX else
                                           str(clamped_val))

                            break

                else:
                    # No default kicked in. If there is an active range
                    # constraint, then the low end of the range is used,
                    # provided it's > 0, with "0x" prepended as appropriate.
                    if has_active_range and low > 0:
                        val = (hex(low) if self._type == HEX else str(low))

        elif self._type == STRING:
            self._write_to_conf = (vis != "n")

            if vis != "n" and self.user_value is not None:
                val = self.user_value
            else:
                for val_expr, cond_expr in self.defaults:
                    if eval_expr(cond_expr) != "n":
                        self._write_to_conf = True
                        val = _str_val(val_expr)
                        break

        self._cached_val = val
        return val

    @property
    def assignable(self):
        """
        See the class documentation.
        """
        if self._cached_assignable is not None:
            return self._cached_assignable

        self._cached_assignable = self._get_assignable()
        return self._cached_assignable

    @property
    def visibility(self):
        """
        See the class documentation.
        """
        if self._cached_vis is not None:
            return self._cached_vis

        self._cached_vis = _get_visibility(self)
        return self._cached_vis

    @property
    def config_string(self):
        """
        See the class documentation.
        """

        if self.env_var is not None:
            # Variables with 'option env' never get written out. This
            # corresponds to the SYMBOL_AUTO flag in the C implementation.
            return None

        # Note: _write_to_conf is determined when the value is calculated
        val = self.value
        if not self._write_to_conf:
            return None

        if self._type in (BOOL, TRISTATE):
            return "{}{}={}\n" \
                   .format(self.config.config_prefix, self.name, val) \
                   if val != "n" else \
                   "# {}{} is not set\n" \
                   .format(self.config.config_prefix, self.name)

        if self._type in (INT, HEX):
            return "{}{}={}\n" \
                   .format(self.config.config_prefix, self.name, val)

        if self._type == STRING:
            # Escape \ and "
            return '{}{}="{}"\n' \
                   .format(self.config.config_prefix, self.name,
                           val.replace("\\", "\\\\").replace('"', '\\"'))

        _internal_error("Internal error while creating .config: unknown "
                        'type "{}".'.format(self._type))

    def set_value(self, value):
        """
        Sets the user value of the symbol.

        Equal in effect to assigning the value to the symbol within a .config
        file. Use the 'assignable' attribute to check which values can
        currently be assigned. Setting values outside 'assignable' will cause
        Symbol.user_value to differ from Symbol.value (be truncated down or
        up). Values that are invalid for the type (such as "foo" or "m" for a
        BOOL) are ignored (and won't be stored in Symbol.user_value). A warning
        is printed for attempts to assign invalid values.

        The values of other symbols that depend on this symbol are
        automatically recalculated to reflect the new value.

        value:
          The user value to give to the symbol.
        """
        self._set_value_no_invalidate(value, False)

        if self is self.config.modules:
            # Changing MODULES has wide-ranging effects
            self.config._invalidate_all()
        else:
            self._rec_invalidate()

    def unset_value(self):
        """
        Resets the user value of the symbol, as if the symbol had never gotten
        a user value via Config.load_config() or Symbol.set_value().
        """
        self.user_value = None
        self._rec_invalidate()

    def __str__(self):
        """
        Returns a string representation of the symbol, matching the Kconfig
        format. As a convenience, prompts and help texts are also printed, even
        though they really belong to the symbol's menu nodes and not to the
        symbol itself.

        The output is designed so that feeding it back to a Kconfig parser
        redefines the symbol as is. This also works for symbols defined in
        multiple locations, where all the definitions are output.

        An empty string is returned for undefined symbols.
        """
        return _sym_choice_str(self)

    def __repr__(self):
        """
        Prints some information about the symbol (including its name, value,
        and visibility) when it is evaluated.
        """
        fields = [
            "symbol " + self.name,
            _TYPENAME[self.type],
            'value "{}"'.format(self.value),
            "visibility {}".format(self.visibility)
        ]

        if self.user_value is not None:
            fields.append('user value "{}"'.format(self.user_value))

        if self.choice is not None:
            fields.append("choice symbol")

        if self.is_allnoconfig_y:
            fields.append("allnoconfig_y")

	if self is self.config.defconfig_list:
            fields.append("is the defconfig_list symbol")

        if self.env_var is not None:
            fields.append("from environment variable " + self.env_var)

        if self is self.config.modules:
            fields.append("is the modules symbol")

        fields.append("direct deps " + eval_expr(self.direct_deps))

        fields.append("{} menu node{}"
                      .format(len(self.nodes),
                              "" if len(self.nodes) == 1 else "s"))

        return "<{}>".format(", ".join(fields))

    #
    # Private methods
    #

    def __init__(self):
        """
        Symbol constructor -- not intended to be called directly by Kconfiglib
        clients.
        """

        # These attributes are always set on the instance from outside and
        # don't need defaults:
        #   config
        #   name
        #   _already_written

        self._type = UNKNOWN
        self.defaults = []
        self.selects = []
        self.implies = []
        self.ranges = []
        self.rev_dep = "n"
        self.weak_rev_dep = "n"

        self.nodes = []

        self.user_value = None

        self.direct_deps = "n"

        # Populated in Config._build_dep() after parsing. Links the symbol to
        # the symbols that immediately depend on it (in a caching/invalidation
        # sense). The total set of dependent symbols for the symbol is
        # calculated as needed in _get_dependent().
        self._direct_dependents = set()

        # Cached values

        # Caches the calculated value
        self._cached_val = None
        # Caches the visibility
        self._cached_vis = None
        # Caches the total list of dependent symbols. Calculated in
        # _get_dependent().
        self._cached_deps = None
        # Caches the 'assignable' attribute
        self._cached_assignable = None

        # Flags

        self.env_var = None

        self.choice = None
        self.is_allnoconfig_y = False

        # Should the symbol get an entry in .config? Calculated along with the
        # value.
        self._write_to_conf = False

    def _get_assignable(self):
        """
        Worker function for the 'assignable' attribute.
        """

        if self._type not in (BOOL, TRISTATE):
            return ""

        vis = self.visibility

        if vis == "n":
            return ""

        rev_dep_val = eval_expr(self.rev_dep)

        if vis == "y":
            if rev_dep_val == "n":
                if self.type == BOOL or eval_expr(self.weak_rev_dep) == "y":
                    return "ny"
                return "nmy"

            if rev_dep_val == "y":
                return "y"

            # rev_dep_val == "m"

            if self.type == BOOL or eval_expr(self.weak_rev_dep) == "y":
                return "y"
            return "my"

        # vis == "m"

        if rev_dep_val == "n":
            return "m" if eval_expr(self.weak_rev_dep) != "y" else "y"

        if rev_dep_val == "y":
            return "y"

        # vis == "m", rev_dep == "m" (rare)

        return "m"

    def _set_value_no_invalidate(self, value, suppress_prompt_warning):
        """
        Like set_value(), but does not invalidate any symbols.

        suppress_prompt_warning:
          The warning about assigning a value to a promptless symbol gets
          spammy for Linux defconfigs, so turn it off when loading .configs.
          It's still helpful when manually invoking set_value().
        """

        # Check if the value is valid for our type
        if not ((self._type == BOOL     and value in ("n", "y")     ) or
                (self._type == TRISTATE and value in ("n", "m", "y")) or
                (self._type == STRING                               ) or
                (self._type == INT      and _is_base_n(value, 10)   ) or
                (self._type == HEX      and _is_base_n(value, 16)   )):
            self.config._warn('the value "{}" is invalid for {}, which has '
                              "type {}. Assignment ignored."
                              .format(value, self.name, _TYPENAME[self._type]))
            return

        if not self.nodes:
            self.config._warn_undef_assign(
                'assigning the value "{}" to the undefined symbol {} will '
                "have no effect".format(value, self.name))

        if not suppress_prompt_warning:
            for node in self.nodes:
                if node.prompt is not None:
                    break
            else:
                self.config._warn('assigning the value "{}" to the '
                                  "promptless symbol {} will have no effect"
                                  .format(value, self.name))

        self.user_value = value

        if self.choice is not None and self._type in (BOOL, TRISTATE):
            if value == "y":
                self.choice.user_selection = self
                self.choice.user_value = "y"
            elif value == "m":
                self.choice.user_value = "m"

    def _invalidate(self):
        """
        Marks the symbol as needing to be recalculated.
        """
        self._cached_val = self._cached_vis = self._cached_assignable = None

    def _rec_invalidate(self):
        """
        Invalidates the symbol and all symbols and choices that (possibly
        indirectly) depend on it
        """
        self._invalidate()

        for item in self._get_dependent():
            item._invalidate()

    def _get_dependent(self):
        """
        Returns the set of symbols that should be invalidated if the value of
        the symbol changes, because they might be affected by the change. Note
        that this is an internal API and probably of limited usefulness to
        clients.
        """
        if self._cached_deps is not None:
            return self._cached_deps

        # Less readable version of the following, measured to reduce the the
        # running time of _get_dependent() on kernel Kconfigs by about 1/3 as
        # measured by line_profiler.
        #
        # res = set(self._direct_dependents)
        # for s in self._direct_dependents:
        #     res |= s._get_dependent()
        res = self._direct_dependents | \
              {sym for dep in self._direct_dependents
                   for sym in dep._get_dependent()}

        if self.choice is not None:
            # Choices depend on their choice symbols
            res.add(self.choice)

            # Choice symbols also depend (recursively) on their siblings. The
            # siblings are not included in _direct_dependents to avoid
            # dependency loops.
            for sibling in self.choice.syms:
                if sibling is not self:
                    res.add(sibling)
                    # Less readable version of the following:
                    #
                    # res |= sibling._direct_dependents
                    # for s in sibling._direct_dependents:
                    #     res |= s._get_dependent()
                    res |= sibling._direct_dependents | \
                           {sym for dep in sibling._direct_dependents
                                for sym in dep._get_dependent()}

        # The tuple conversion sped up allnoconfig_simpler.py by 10%
        self._cached_deps = tuple(res)
        return self._cached_deps

class Choice(object):
    """
    Represents a choice statement:

      choice
          ...

    The following attributes are available on Choice instances. They should be
    viewed as read-only, and some are implemented through @property magic (but
    are still efficient to access due to internal caching).

    name:
      The name of the choice, e.g. "FOO" for 'choice FOO', or None if the
      Choice has no name. I can't remember ever seeing named choices in
      practice, but the C tools support them too.

    type:
      The type of the choice. One of BOOL, TRISTATE, UNKNOWN. UNKNOWN is for
      choices defined without a type where none of the contained symbols have a
      type either (otherwise the choice inherits the type of the first symbol
      defined with a type).

      When running without modules (CONFIG_MODULES=n), TRISTATE choices
      magically change type to BOOL. This matches the C tools, and makes sense
      for menuconfig-like functionality. (Check the implementation of the
      property if you need to get the original type.)

    value:
      The tristate value (mode) of the choice. A choice can be in one of three
      modes:

        "n" - The choice is not visible and no symbols can be selected.

        "m" - Any number of symbols can be set to "m". The rest will be "n".

        "y" - One symbol will be "y" while the rest are "n".

      Only tristate choices can be in "m" mode, and the visibility of the
      choice is an upper bound on the mode.

      The mode changes automatically when a value is assigned to a symbol
      within the choice (this makes .config loading "just work"), and can also
      be changed via Choice.set_value().

      See the implementation note at the end for one reason why it makes sense
      to call this 'value' rather than e.g. 'mode'. It also makes the Choice
      and Symbol interfaces consistent.

    assignable:
      See the symbol class documentation. Gives the assignable values (modes).

    visibility:
      See the Symbol class documentation. Acts on the value (mode).

    selection:
      The currently selected symbol. None if the Choice is not in "y" mode or
      has no selected symbol (due to unsatisfied dependencies on choice
      symbols).

    default_selection:
      The symbol that would be selected by default, had the user not selected
      any symbol. Can be None for the same reasons as 'selected'.

    user_value:
      The value (mode) selected by the user (by assigning some choice symbol or
      calling Choice.set_value()). This does not necessarily match Choice.value
      for the same reasons that Symbol.user_value might not match Symbol.value.

    user_selection:
      The symbol selected by the user (by setting it to "y"). Ignored if the
      choice is not in "y" mode, but still remembered so that the choice "snaps
      back" to the user selection if the mode is changed back to "y".

    syms:
      List of symbols contained in the choice.

      Gotcha: If a symbol depends on a previous symbol within a choice so that
      an implicit menu is created, it won't be a choice symbol, and won't be
      included in 'syms'. There are real-world examples of this.

    nodes:
      A list of MenuNode's for this symbol. In practice, the list will probably
      always contain a single MenuNode, but it is possible to define a choice
      in multiple locations by giving it a name, which adds more nodes.

    defaults:
      List of (symbol, cond) tuples for the choices 'defaults's. For example,
      'default A if B && C' is represented as (A, (AND, B, C)). If there is no
      condition, 'cond' is None.

      Note that 'depends on' and parent dependencies are propagated to
      'default' conditions.

    is_optional:
      True if the choice has the 'optional' flag set on it.

    Implementation note: The C tools internally represent choices as a type of
    symbol, with special-casing in many code paths, which is why there is a lot
    of similarity to Symbol above. The value (mode) is really just a normal
    symbol value, and an implicit reverse dependency forces its lower bound to
    'm' for non-optional choices. Kconfiglib uses a separate Choice class only
    because it makes the code and interface less confusing (especially in a
    user-facing interface).
    """

    __slots__ = (
        "_cached_assignable",
        "_cached_selection",
        "_cached_vis",
        "_type",
        "config",
        "defaults",
        "is_optional",
        "name",
        "nodes",
        "syms",
        "user_selection",
        "user_value",
    )

    #
    # Public interface
    #

    @property
    def type(self):
        """Returns the type of the choice. See Symbol.type."""
        if self._type == TRISTATE and self.config.modules.value == "n":
            return BOOL
        return self._type

    @property
    def value(self):
        """
        See the class documentation.
        """
        if self.user_value is not None:
            val = _eval_min(self.user_value, self.visibility)
        else:
            val = "n"

        if val == "n" and not self.is_optional:
            val = "m"

        # Promote "m" to "y" for boolean choices
        return "y" if val == "m" and self.type == BOOL else val

    @property
    def assignable(self):
        """
        See the class documentation.
        """
        if self._cached_assignable is not None:
            return self._cached_assignable

        self._cached_assignable = self._get_assignable()
        return self._cached_assignable

    @property
    def visibility(self):
        """
        See the class documentation.
        """
        if self._cached_vis is not None:
            return self._cached_vis

        self._cached_vis = _get_visibility(self)
        return self._cached_vis

    @property
    def selection(self):
        """
        See the class documentation.
        """
        if self._cached_selection is not _NO_CACHED_SELECTION:
            return self._cached_selection

        if self.value != "y":
            self._cached_selection = None
            return None

        # User choice available?
        if self.user_selection is not None and \
           self.user_selection.visibility == "y":
            self._cached_selection = self.user_selection
            return self.user_selection

        # Look at defaults

        self._cached_selection = self.default_selection
        return self._cached_selection

    @property
    def default_selection(self):
        """
        See the class documentation.
        """
        for sym, cond_expr in self.defaults:
            if (eval_expr(cond_expr) != "n" and
                # Must be visible too
                sym.visibility != "n"):
                return sym

        # Otherwise, pick the first visible symbol, if any
        for sym in self.syms:
            if sym.visibility != "n":
                return sym

        # Couldn't find a default
        return None

    def set_value(self, value):
        """
        Sets the user value (mode) of the choice. Like for Symbol.set_value(),
        the visibility might truncate the value. Choices without the 'optional'
        attribute (is_optional) can never be in "n" mode, but "n" is still
        accepted (and ignored) since it's not a malformed value.
        """
        if not ((self._type == BOOL     and value in ("n", "y")    ) or
                (self._type == TRISTATE and value in ("n", "m", "y"))):
            self.config._warn('the value "{}" is invalid for the choice, '
                              "which has type {}. Assignment ignored"
                              .format(value, _TYPENAME[self._type]))

        self.user_value = value

        if self.syms:
            # Hackish way to invalidate the choice and all the choice symbols
            self.syms[0]._rec_invalidate()

    def unset_value(self):
        """
        Resets the user value (mode) and user selection of the Choice, as if
        the user had never touched the mode or any of the choice symbols.
        """
        self.user_value = self.user_selection = None
        if self.syms:
            # Hackish way to invalidate the choice and all the choice symbols
            self.syms[0]._rec_invalidate()

    def __str__(self):
        """
        Returns a string containing various information about the choice
        statement.
        """
        return _sym_choice_str(self)

    def __repr__(self):
        fields = [
            "choice" if self.name is None else "choice " + self.name,
            _TYPENAME[self.type],
            "mode " + self.value,
            "visibility " + self.visibility]

        if self.is_optional:
            fields.append("optional")

        if self.selection is not None:
            fields.append("{} selected".format(self.selection.name))

        fields.append("{} menu node{}"
                      .format(len(self.nodes),
                              "" if len(self.nodes) == 1 else "s"))

        return "<{}>".format(", ".join(fields))


    #
    # Private methods
    #

    def __init__(self):
        """
        Choice constructor -- not intended to be called directly by Kconfiglib
        clients.
        """

        # These attributes are always set on the instance from outside and
        # don't need defaults:
        #   config

        self.name = None
        self._type = UNKNOWN
        self.syms = []
        self.defaults = []

        self.nodes = []

        self.user_selection = None
        self.user_value = None

        # The prompts and default values without any dependencies from
        # enclosing menus and ifs propagated
        self.defaults = []

        # Cached values
        self._cached_selection = _NO_CACHED_SELECTION
        self._cached_vis = None
        self._cached_assignable = None

        self.is_optional = False

    def _get_assignable(self):
        """
        Worker function for the 'assignable' attribute.
        """

        vis = self.visibility

        if vis == "n":
            return ""

        if vis == "y":
            if not self.is_optional:
                return "y" if self.type == BOOL else "my"
            return "y"

        # vis == "m"

        return "nm" if self.is_optional else "m"

    def _invalidate(self):
        self._cached_selection = _NO_CACHED_SELECTION
        self._cached_vis = self._cached_assignable = None

class MenuNode(object):
    """
    Represents a menu node in the configuration. This corresponds to an entry
    in e.g. the 'make menuconfig' interface, though non-visible,
    non-user-assignable symbols, choices, menus, and comments also get menu
    nodes. If a symbol or choice is defined in multiple locations, it gets one
    menu node for each location.

    The top-level menu node, corresponding to the implicit top-level menu, is
    available in Config.top_menu.

    For symbols and choices, the menu nodes are available in the 'nodes'
    attribute. Menus and comments are represented as plain menu nodes, with
    their text stored in the prompt attribute (prompt[0]). This mirrors the C
    implementation.

    The following attributes are available on MenuNode instances. They should
    be viewed as read-only.

    item:
        Either a Symbol, a Choice, or one of the constants MENU and COMMENT.
        Menus and comments are represented as plain menu nodes. Ifs are
        collapsed and do not appear in the final menu tree (matching the C
        implementation).

    next:
        The following menu node in the menu tree. None if there is no following
        node.

    list:
        The first child menu node in the menu tree. None if there are no
        children.

        Choices and menus naturally have children, but Symbols can have
        children too because of menus created automatically from dependencies
        (see kconfig-language.txt).

    parent:
        The parent menu node. None if there is no parent.

    prompt:
        A (string, cond) tuple with the prompt for the menu node and its
        condition. None if there is no prompt. Prompts are always stored in the
        menu node rather than the Symbol or Choice. For menus and comments, the
        prompt holds the text.

    help:
        The help text for the menu node. None if there is no help text. Always
        stored in the node rather than the Symbol or Choice. It is possible to
        have a separate help at each location if a symbol is defined in
        multiple locations.

    dep:
        The 'depends on' dependencies for the menu node. None if there are no
        dependencies. Parent dependencies are propagated to this attribute, and
        this attribute is then in turn propagated to the properties of symbols
        and choices.

        If a symbol is defined in multiple locations, only the properties
        defined at each location get the corresponding MenuNode.dep propagated
        to them.

    visibility:
        The 'visible if' dependencies for the menu node (which must represent a
        menu). None if there are no 'visible if' dependencies. 'visible if'
        dependencies are recursively propagated to the prompts of symbols and
        choices within the menu.

    is_menuconfig:
        True if the symbol for the menu node (it must be a symbol) was defined
        with 'menuconfig' rather than 'config' (at this location). This is a
        hint on how to display the menu entry. It's ignored by Kconfiglib
        itself.

    config:
        The Config the menu node is from.

    filename/linenr:
        The location where the menu node appears.
    """

    __slots__ = (
        "config",
        "dep",
        "filename",
        "help",
        "is_menuconfig",
        "item",
        "linenr",
        "list",
        "next",
        "parent",
        "prompt",
        "visibility",
    )

    def __repr__(self):
        fields = []

        if isinstance(self.item, Symbol):
            fields.append("menu node for symbol " + self.item.name)
        elif isinstance(self.item, Choice):
            s = "menu node for choice"
            if self.item.name is not None:
                s += " " + self.item.name
            fields.append(s)
        elif self.item == MENU:
            fields.append("menu node for menu")
        elif self.item == COMMENT:
            fields.append("menu node for comment")
        elif self.item is None:
            fields.append("menu node for if (should not appear in the final "
                          " tree)")
        else:
            raise InternalError("unable to determine type in "
                                "MenuNode.__repr__()")

        fields.append("{}:{}".format(self.filename, self.linenr))

        if self.prompt is not None:
            fields.append('prompt "{}" (visibility {})'
                          .format(self.prompt[0], eval_expr(self.prompt[1])))

        if isinstance(self.item, Symbol) and self.is_menuconfig:
            fields.append("is menuconfig")

        fields.append("deps " + eval_expr(self.dep))

        if self.item == MENU:
            fields.append("'visible if' deps " + eval_expr(self.visibility))

        if isinstance(self.item, (Symbol, Choice)) and self.help is not None:
            fields.append("has help")

        if self.list is not None:
            fields.append("has child")

        if self.next is not None:
            fields.append("has next")

        return "<{}>".format(", ".join(fields))

class KconfigSyntaxError(Exception):
    """
    Exception raised for syntax errors.
    """
    pass

class InternalError(Exception):
    """
    Exception raised for internal errors.
    """
    pass

#
# Public functions
#

def tri_less(v1, v2):
    """
    Returns True if the tristate v1 is less than the tristate v2, where "n",
    "m" and "y" are ordered from lowest to highest.
    """
    return _TRI_TO_INT[v1] < _TRI_TO_INT[v2]

def tri_less_eq(v1, v2):
    """
    Returns True if the tristate v1 is less than or equal to the tristate v2,
    where "n", "m" and "y" are ordered from lowest to highest.
    """
    return _TRI_TO_INT[v1] <= _TRI_TO_INT[v2]

def tri_greater(v1, v2):
    """
    Returns True if the tristate v1 is greater than the tristate v2, where "n",
    "m" and "y" are ordered from lowest to highest.
    """
    return _TRI_TO_INT[v1] > _TRI_TO_INT[v2]

def tri_greater_eq(v1, v2):
    """
    Returns True if the tristate v1 is greater than or equal to the tristate
    v2, where "n", "m" and "y" are ordered from lowest to highest.
    """
    return _TRI_TO_INT[v1] >= _TRI_TO_INT[v2]

# Expression evaluation

def eval_expr(expr):
    """
    Evaluates an expression to "n", "m", or "y". Returns "y" for None, which
    makes sense as None usually indicates a missing condition.
    """
    return "y" if expr is None else _eval_expr_rec(expr)

def _eval_expr_rec(expr):
    if isinstance(expr, Symbol):
        # Non-bool/tristate symbols are always "n" in a tristate sense,
        # regardless of their value
        return expr.value if expr._type in (BOOL, TRISTATE) else "n"

    if isinstance(expr, str):
        return expr if expr in ("m", "y") else "n"

    if expr[0] == _AND:
        ev1 = _eval_expr_rec(expr[1])
        if ev1 == "n":
            # No need to look at expr[2]
            return "n"
        ev2 = _eval_expr_rec(expr[2])
        return ev2 if ev1 == "y" else \
               "m" if ev2 != "n" else \
               "n"

    if expr[0] == _OR:
        ev1 = _eval_expr_rec(expr[1])
        if ev1 == "y":
            # No need to look at expr[2]
            return "y"
        ev2 = _eval_expr_rec(expr[2])
        return ev2 if ev1 == "n" else \
               "y" if ev2 == "y" else \
               "m"

    if expr[0] == _NOT:
        ev = _eval_expr_rec(expr[1])
        return "n" if ev == "y" else \
               "y" if ev == "n" else \
               "m"

    if expr[0] in _RELATIONS:
        # Implements <, <=, >, >= comparisons as well. These were added to
        # kconfig in 31847b67 (kconfig: allow use of relations other than
        # (in)equality).

        # This mirrors the C tools pretty closely. Perhaps there's a more
        # pythonic way to structure this.

        oper, op1, op2 = expr
        op1_type, op1_str = _type_and_val(op1)
        op2_type, op2_str = _type_and_val(op2)

        # If both operands are strings...
        if op1_type == STRING and op2_type == STRING:
            # ...then compare them lexicographically
            comp = _strcmp(op1_str, op2_str)
        else:
            # Otherwise, try to compare them as numbers
            try:
                comp = int(op1_str, _TYPE_TO_BASE[op1_type]) - \
                       int(op2_str, _TYPE_TO_BASE[op2_type])
            except ValueError:
                # They're not both valid numbers. If the comparison is
                # anything but = or !=, return 'n'. Otherwise, reuse
                # _strcmp() to check for (in)equality.
                if oper not in (_EQUAL, _UNEQUAL):
                    return "n"
                comp = _strcmp(op1_str, op2_str)

        if   oper == _EQUAL:         res = comp == 0
        elif oper == _UNEQUAL:       res = comp != 0
        elif oper == _LESS:          res = comp < 0
        elif oper == _LESS_EQUAL:    res = comp <= 0
        elif oper == _GREATER:       res = comp > 0
        elif oper == _GREATER_EQUAL: res = comp >= 0

        return "y" if res else "n"

    _internal_error("Internal error while evaluating expression: "
                    "unknown operation {}.".format(expr[0]))


#
# Internal classes
#

class _Feed(object):
    """
    Class for working with sequences in a stream-like fashion; handy for
    tokens.
    """

    __slots__ = (
        'i',
        'length',
        'items',
    )

    def __init__(self, items):
        self.items = items
        self.length = len(self.items)
        self.i = 0

    def next(self):
        if self.i >= self.length:
            return None
        item = self.items[self.i]
        self.i += 1
        return item

    def peek(self):
        return None if self.i >= self.length else self.items[self.i]

    def check(self, token):
        """
        Checks if the next token is 'token'. If so, removes it from the token
        feed and return True. Otherwise, leaves it in and return False.
        """
        if self.i < self.length and self.items[self.i] == token:
            self.i += 1
            return True
        return False

class _FileFeed(object):
    """
    Feeds lines from a file. Keeps track of the filename and current line
    number. Joins any line ending in \\ with the following line. We need to be
    careful to get the line number right in the presence of continuation lines.
    """

    __slots__ = (
        'filename',
        'lines',
        'length',
        'linenr'
    )

    def __init__(self, file_, filename):
        self.filename = filename
        with file_:
            # No interleaving of I/O and processing yet. Don't know if it would
            # help.
            self.lines = file_.readlines()
        self.length = len(self.lines)
        self.linenr = 0

    def next(self):
        if self.linenr >= self.length:
            return None
        line = self.lines[self.linenr]
        self.linenr += 1
        while line.endswith("\\\n"):
            line = line[:-2] + self.lines[self.linenr]
            self.linenr += 1
        return line

    def next_no_join(self):
        if self.linenr >= self.length:
            return None
        line = self.lines[self.linenr]
        self.linenr += 1
        return line

#
# Internal functions
#

def _get_visibility(sc):
    """
    Symbols and Choices have a "visibility" that acts as an upper bound on the
    values a user can set for them, corresponding to the visibility in e.g.
    'make menuconfig'. This function calculates the visibility for the Symbol
    or Choice 'sc' -- the logic is nearly identical.
    """
    vis = "n"

    for node in sc.nodes:
        if node.prompt:
            vis = _eval_max(vis, node.prompt[1])

    if isinstance(sc, Symbol) and sc.choice is not None:
        if sc.choice._type == TRISTATE and sc._type != TRISTATE and \
           sc.choice.value != "y":
            # Non-tristate choice symbols in tristate choices depend on the
            # choice being in mode "y"
            return "n"

        if sc._type == TRISTATE and vis == "m" and sc.choice.value == "y":
            # Choice symbols with visibility "m" are not visible if the
            # choice has mode "y"
            return "n"

        vis = _eval_min(vis, sc.choice.visibility)

    # Promote "m" to "y" if we're dealing with a non-tristate. This might lead
    # to infinite recursion if something really weird is done with MODULES, but
    # it's not a problem in practice.
    if vis == "m" and \
       (sc._type != TRISTATE or sc.config.modules.value == "n"):
        return "y"

    return vis

def _make_and(e1, e2):
    """
    Constructs an _AND (&&) expression. Performs trivial simplification. Nones
    equate to 'y'.

    Returns None if e1 == e2 == None, so that ANDing two nonexistent
    expressions gives a nonexistent expression.
    """
    if e1 is None or e1 == "y":
        return e2
    if e2 is None or e2 == "y":
        return e1
    return (_AND, e1, e2)

def _make_or(e1, e2):
    """
    Constructs an _OR (||) expression. Performs trivial simplification and
    avoids Nones. Nones equate to 'y', which is usually what we want, but needs
    to be kept in mind.
    """

    # Perform trivial simplification and avoid None's (which
    # correspond to y's)
    if e1 is None or e2 is None or e1 == "y" or e2 == "y":
        return "y"
    if e1 == "n":
        return e2
    return (_OR, e1, e2)

def _eval_min(e1, e2):
    """
    Returns the minimum value of the two expressions. Equates None with 'y'.
    """
    e1_eval = eval_expr(e1)
    e2_eval = eval_expr(e2)
    return e1_eval if tri_less(e1_eval, e2_eval) else e2_eval

def _eval_max(e1, e2):
    """
    Returns the maximum value of the two expressions. Equates None with 'y'.
    """
    e1_eval = eval_expr(e1)
    e2_eval = eval_expr(e2)
    return e1_eval if tri_greater(e1_eval, e2_eval) else e2_eval

def _expr_syms_rec(expr, res):
    """
    _expr_syms() helper. Recurses through expressions.
    """
    if isinstance(expr, Symbol):
        res.append(expr)
    elif isinstance(expr, str):
        return
    elif expr[0] in (_AND, _OR):
        _expr_syms_rec(expr[1], res)
        _expr_syms_rec(expr[2], res)
    elif expr[0] == _NOT:
        _expr_syms_rec(expr[1], res)
    elif expr[0] in _RELATIONS:
        if isinstance(expr[1], Symbol):
            res.append(expr[1])
        if isinstance(expr[2], Symbol):
            res.append(expr[2])
    else:
        _internal_error("Internal error while fetching symbols from an "
                        "expression with token stream {}.".format(expr))

def _expr_syms(expr, res):
    """
    append()s the symbols in 'expr' to 'res'. Does not remove duplicates.
    """
    if expr is not None:
        _expr_syms_rec(expr, res)

def _str_val(obj):
    """
    Returns the value of obj as a string. If obj is not a string (constant
    symbol), it must be a Symbol.
    """
    return obj if isinstance(obj, str) else obj.value

def _format_and_op(expr):
    """
    _expr_to_str() helper. Returns the string representation of 'expr', which
    is assumed to be an operand to _AND, with parentheses added if needed.
    """
    if isinstance(expr, tuple) and expr[0] == _OR:
        return "({})".format(_expr_to_str(expr))
    return _expr_to_str(expr)

def _expr_to_str(expr):
    if isinstance(expr, str):
        if expr in ("n", "m", "y"):
            # Don't print spammy quotes for these
            return expr
        return '"{}"'.format(expr)

    if isinstance(expr, Symbol):
        return expr.name

    if expr[0] == _NOT:
        if isinstance(expr[1], (str, Symbol)):
            return "!" + _expr_to_str(expr[1])
        return "!({})".format(_expr_to_str(expr[1]))

    if expr[0] == _AND:
        return "{} && {}".format(_format_and_op(expr[1]),
                                 _format_and_op(expr[2]))

    if expr[0] == _OR:
        return "{} || {}".format(_expr_to_str(expr[1]),
                                 _expr_to_str(expr[2]))

    # Relation
    return "{} {} {}".format(_expr_to_str(expr[1]),
                             _RELATION_TO_STR[expr[0]],
                             _expr_to_str(expr[2]))

def _type_and_val(obj):
    """
    Helper to hack around the fact that we don't represent plain strings as
    Symbols. Takes either a plain string or a Symbol and returns a (<type>,
    <value>) tuple.
    """
    return (obj._type, obj.value) \
           if not isinstance(obj, str) \
           else (STRING, obj)

def _indentation(line):
    """
    Returns the length of the line's leading whitespace, treating tab stops as
    being spaced 8 characters apart.
    """
    line = line.expandtabs()
    return len(line) - len(line.lstrip())

def _deindent(line, indent):
    """
    Deindents 'line' by 'indent' spaces.
    """
    line = line.expandtabs()
    if len(line) <= indent:
        return line
    return line[indent:]

def _is_base_n(s, n):
    try:
        int(s, n)
        return True
    except ValueError:
        return False

def _strcmp(s1, s2):
    """
    strcmp()-alike that returns -1, 0, or 1.
    """
    return (s1 > s2) - (s1 < s2)

def _lines(*args):
    """
    Returns a string consisting of all arguments, with newlines inserted
    between them.
    """
    return "\n".join(args)

def _stderr_msg(msg, filename, linenr):
    if filename is not None:
        sys.stderr.write("{}:{}: ".format(filename, linenr))
    sys.stderr.write(msg + "\n")

def _tokenization_error(s, filename, linenr):
    loc = "" if filename is None else "{}:{}: ".format(filename, linenr)
    raise KconfigSyntaxError("{}Couldn't tokenize '{}'"
                               .format(loc, s.strip()))

def _parse_error(s, msg, filename, linenr):
    loc = "" if filename is None else "{}:{}: ".format(filename, linenr)
    raise KconfigSyntaxError("{}Couldn't parse '{}'{}"
                             .format(loc, s.strip(),
                                     "." if msg is None else ": " + msg))

def _internal_error(msg):
    raise InternalError(
        msg +
        "\nSorry! You may want to send an email to ulfalizer a.t Google's "
        "email service to tell me about this. Include the message above and "
        "the stack trace and describe what you were doing.")

# Printing functions

def _sym_choice_str(sc):
    """
    Symbol/choice __str__() implementation. These have many properties in
    common, so it makes sense to handle them together.
    """
    lines = []

    def indent_add(s):
        lines.append("\t" + s)

    # We print the prompt(s) and help text(s) too as a convenience, even though
    # they're actually part of the menu node. If a symbol or choice is defined
    # in multiple locations (has more than one menu node), we output one
    # statement for each location, and print all the properties that belong to
    # the symbol/choice itself only at the first location. This gives output
    # that would function if fed to a Kconfig parser, even for such
    # symbols/choices (choices defined in multiple locations gets iffy since
    # they also have child nodes, but I've never seen such a choice).

    if not sc.nodes:
        return ""

    for node in sc.nodes:
        if isinstance(sc, Symbol):
            if node.is_menuconfig:
                lines.append("menuconfig " + sc.name)
            else:
                lines.append("config " + sc.name)
        else:
            if sc.name is None:
                lines.append("choice")
            else:
                lines.append("choice " + sc.name)

        if node is sc.nodes[0] and sc.type != UNKNOWN:
            indent_add(_TYPENAME[sc.type])

        if node.prompt is not None:
            prompt_str = 'prompt "{}"'.format(node.prompt[0])
            if node.prompt[1] is not None:
                prompt_str += " if " + _expr_to_str(node.prompt[1])
            indent_add(prompt_str)

        if node is sc.nodes[0]:
            if isinstance(sc, Symbol):
                if sc.is_allnoconfig_y:
                    indent_add("option allnoconfig_y")
		if sc is sc.config.defconfig_list:
                    indent_add("option defconfig_list")
                if sc.env_var is not None:
                    indent_add('option env="{}"'.format(sc.env_var))
		if sc is sc.config.modules:
                    indent_add("option modules")

            if isinstance(sc, Symbol):
                for range_ in sc.ranges:
                    range_string = "range {} {}" \
                                   .format(_expr_to_str(range_[0]),
                                           _expr_to_str(range_[1]))
                    if range_[2] is not None:
                        range_string += " if " + _expr_to_str(range_[2])
                    indent_add(range_string)

            for default in sc.defaults:
                default_string = "default " + _expr_to_str(default[0])
                if default[1] is not None:
                    default_string += " if " + _expr_to_str(default[1])
                indent_add(default_string)

            if isinstance(sc, Choice) and sc.is_optional:
                indent_add("optional")

            if isinstance(sc, Symbol):
                for select in sc.selects:
                    select_string = "select " + select[0].name
                    if select[1] is not None:
                        select_string += " if " + _expr_to_str(select[1])
                    indent_add(select_string)

                for imply in sc.implies:
                    imply_string = "imply " + imply[0].name
                    if imply[1] is not None:
                        imply_string += " if " + _expr_to_str(imply[1])
                    indent_add(imply_string)

        if node.help is not None:
            indent_add("help")
            for line in node.help.splitlines():
                indent_add("  " + line)

        # Add a blank line if there are more nodes to print
        if node is not sc.nodes[-1]:
            lines.append("")

    return "\n".join(lines) + "\n"

# Menu manipulation

def _eq_to_sym(eq):
    """
    _expr_depends_on() helper. For (in)equalities of the form sym = y/m or
    sym != n, returns sym. For other (in)equalities, returns None.
    """
    relation, left, right = eq

    # Make sure the symbol (if any) appears to the left
    if not isinstance(left, Symbol):
        left, right = right, left
    if not isinstance(left, Symbol):
        return None
    if (relation == _EQUAL and right in ("m", "y")) or \
       (relation == _UNEQUAL and right == "n"):
        return left
    return None

def _expr_depends_on(expr, sym):
    """
    Reimplementation of expr_depends_symbol() from mconf.c. Used to
    determine if a submenu should be implicitly created, which influences
    what items inside choice statements are considered choice items.
    """
    if expr is None:
        return False

    def rec(expr):
        if isinstance(expr, str):
            return False
        if isinstance(expr, Symbol):
            return expr is sym

        if expr[0] in (_EQUAL, _UNEQUAL):
            return _eq_to_sym(expr) is sym
        if expr[0] == _AND:
            return rec(expr[1]) or rec(expr[2])
        return False

    return rec(expr)

def _has_auto_menu_dep(node1, node2):
    """
    Returns True if node2 has an "automatic menu dependency" on node1. If node2
    has a prompt, we check its condition. Otherwise, we look directly at
    node2.dep.
    """

    if node2.prompt:
        return _expr_depends_on(node2.prompt[1], node1.item)

    # If we have no prompt, use the menu node dependencies instead
    return node2.dep is not None and \
           _expr_depends_on(node2.dep, node1.item)

def _check_auto_menu(node):
    """
    Looks for menu nodes after 'node' that depend on it. Creates an implicit
    menu rooted at 'node' with the nodes as the children if such nodes are
    found. The recursive call to _finalize_tree() makes this work recursively.
    """

    cur = node
    while cur.next is not None and \
          _has_auto_menu_dep(node, cur.next):
        _finalize_tree(cur.next)
        cur = cur.next
        cur.parent = node

    if cur is not node:
        node.list = node.next
        node.next = cur.next
        cur.next = None

def _flatten(node):
    """
    "Flattens" menu nodes without prompts (e.g. 'if' nodes and non-visible
    symbols with children from automatic menu creation) so that their children
    appear after them instead. This gives a clean menu structure with no
    unexpected "jumps" in the indentation.
    """

    while node is not None:
        if node.list is not None and \
           (node.prompt is None or node.prompt == ""):

            last_node = node.list
            while 1:
                last_node.parent = node.parent
                if last_node.next is None:
                    break
                last_node = last_node.next

            last_node.next = node.next
            node.next = node.list
            node.list = None

        node = node.next

def _remove_if(node):
    """
    Removes 'if' nodes (which can be recognized by MenuNode.item being None),
    which are assumed to already have been flattened. The C implementation
    doesn't bother to do this, but we expose the menu tree directly, and it
    makes it nicer to work with.
    """
    first = node.list
    while first is not None and first.item is None:
        first = first.next

    cur = first
    while cur is not None:
        if cur.next is not None and cur.next.item is None:
            cur.next = cur.next.next
        cur = cur.next

    node.list = first

def _finalize_choice(node):
    """
    Finalizes a choice, marking each symbol whose menu node has the choice as
    the parent as a choice symbol, and automatically determining types if not
    specified.
    """
    choice = node.item

    cur = node.list
    while cur is not None:
        if isinstance(cur.item, Symbol):
            cur.item.choice = choice
            choice.syms.append(cur.item)
        cur = cur.next

    # If no type is specified for the choice, its type is that of
    # the first choice item with a specified type
    if choice._type == UNKNOWN:
        for item in choice.syms:
            if item._type != UNKNOWN:
                choice._type = item._type
                break

    # Each choice item of UNKNOWN type gets the type of the choice
    for item in choice.syms:
        if item._type == UNKNOWN:
            item._type = choice._type

def _finalize_tree(node):
    """
    Creates implicit menus from dependencies (see kconfig-language.txt),
    removes 'if' nodes, and finalizes choices. This pretty closely mirrors
    menu_finalize() from the C implementation, though we propagate dependencies
    during parsing instead.
    """

    # The ordering here gets a bit tricky, but it's important to do things in
    # this order to have everything work out correctly.

    if node.list is not None:
        # The menu node has children. Finalize them.
        cur = node.list
        while cur is not None:
            _finalize_tree(cur)
            # Note: _finalize_tree() might have changed cur.next. This is
            # expected, so that we jump over e.g. implicitly created submenus.
            cur = cur.next

    elif node.item is not None:
        # The menu node has no children (yet). See if we can create an implicit
        # menu rooted at it (due to menu nodes after it depending on it).
        _check_auto_menu(node)

    if node.list is not None:
        # We have a node with finalized children. Do final steps to finalize
        # this node.
        _flatten(node.list)
        _remove_if(node)

    # Empty choices (node.list None) are possible, so this needs to go outside
    if isinstance(node.item, Choice):
        _finalize_choice(node)

#
# Public global constants
#

# Integers representing menu and comment nodes

(
    MENU,
    COMMENT,
) = range(2)

# Integers representing symbol types
(
    BOOL,
    HEX,
    INT,
    STRING,
    TRISTATE,
    UNKNOWN
) = range(6)

#
# Internal global constants
#

# Tokens
(
    _T_ALLNOCONFIG_Y,
    _T_AND,
    _T_BOOL,
    _T_CHOICE,
    _T_CLOSE_PAREN,
    _T_COMMENT,
    _T_CONFIG,
    _T_DEFAULT,
    _T_DEFCONFIG_LIST,
    _T_DEF_BOOL,
    _T_DEF_TRISTATE,
    _T_DEPENDS,
    _T_ENDCHOICE,
    _T_ENDIF,
    _T_ENDMENU,
    _T_ENV,
    _T_EQUAL,
    _T_GREATER,
    _T_GREATER_EQUAL,
    _T_HELP,
    _T_HEX,
    _T_IF,
    _T_IMPLY,
    _T_INT,
    _T_LESS,
    _T_LESS_EQUAL,
    _T_MAINMENU,
    _T_MENU,
    _T_MENUCONFIG,
    _T_MODULES,
    _T_NOT,
    _T_ON,
    _T_OPEN_PAREN,
    _T_OPTION,
    _T_OPTIONAL,
    _T_OR,
    _T_PROMPT,
    _T_RANGE,
    _T_SELECT,
    _T_SOURCE,
    _T_STRING,
    _T_TRISTATE,
    _T_UNEQUAL,
    _T_VISIBLE,
) = range(44)

# Keyword to token map. Note that the get() method is assigned directly as a
# small optimization.
_get_keyword = {
    "allnoconfig_y":  _T_ALLNOCONFIG_Y,
    "bool":           _T_BOOL,
    "boolean":        _T_BOOL,
    "choice":         _T_CHOICE,
    "comment":        _T_COMMENT,
    "config":         _T_CONFIG,
    "def_bool":       _T_DEF_BOOL,
    "def_tristate":   _T_DEF_TRISTATE,
    "default":        _T_DEFAULT,
    "defconfig_list": _T_DEFCONFIG_LIST,
    "depends":        _T_DEPENDS,
    "endchoice":      _T_ENDCHOICE,
    "endif":          _T_ENDIF,
    "endmenu":        _T_ENDMENU,
    "env":            _T_ENV,
    "help":           _T_HELP,
    "hex":            _T_HEX,
    "if":             _T_IF,
    "imply":          _T_IMPLY,
    "int":            _T_INT,
    "mainmenu":       _T_MAINMENU,
    "menu":           _T_MENU,
    "menuconfig":     _T_MENUCONFIG,
    "modules":        _T_MODULES,
    "on":             _T_ON,
    "option":         _T_OPTION,
    "optional":       _T_OPTIONAL,
    "prompt":         _T_PROMPT,
    "range":          _T_RANGE,
    "select":         _T_SELECT,
    "source":         _T_SOURCE,
    "string":         _T_STRING,
    "tristate":       _T_TRISTATE,
    "visible":        _T_VISIBLE,
}.get

# Tokens after which identifier-like lexemes are treated as strings. _T_CHOICE
# is included to avoid symbols being registered for named choices.
_STRING_LEX = frozenset((
    _T_BOOL,
    _T_CHOICE,
    _T_COMMENT,
    _T_HEX,
    _T_INT,
    _T_MAINMENU,
    _T_MENU,
    _T_PROMPT,
    _T_SOURCE,
    _T_STRING,
    _T_TRISTATE,
))

# Note: This hack is no longer needed as of upstream commit c226456
# (kconfig: warn of unhandled characters in Kconfig commands). It
# is kept around for backwards compatibility.
#
# The initial word on a line is parsed specially. Let
# command_chars = [A-Za-z0-9_]. Then
#  - leading non-command_chars characters are ignored, and
#  - the first token consists the following one or more
#    command_chars characters.
# This is why things like "----help--" are accepted.
#
# In addition to the initial token, the regex also matches trailing whitespace
# so that we can jump straight to the next token (or to the end of the line if
# there's just a single token).
#
# As an optimization, this regex fails to match for lines containing just a
# comment.
_initial_token_re_match = re.compile(r"[^\w#]*(\w+)\s*").match

# Matches an identifier/keyword, also eating trailing whitespace
_id_keyword_re_match = re.compile(r"([\w./-]+)\s*").match

# Regular expression for finding $-references to symbols in strings
_sym_ref_re_search = re.compile(r"\$([A-Za-z0-9_]+)").search

# Strings to use for types
_TYPENAME = {
    UNKNOWN: "unknown",
    BOOL: "bool",
    TRISTATE: "tristate",
    STRING: "string",
    HEX: "hex",
    INT: "int",
}

# Token to type mapping
_TOKEN_TO_TYPE = {
    _T_BOOL:         BOOL,
    _T_DEF_BOOL:     BOOL,
    _T_DEF_TRISTATE: TRISTATE,
    _T_HEX:          HEX,
    _T_INT:          INT,
    _T_STRING:       STRING,
    _T_TRISTATE:     TRISTATE,
}

# Default values for symbols of different types (the value the symbol gets if
# it is not assigned a user value and none of its 'default' clauses kick in)
_DEFAULT_VALUE = {
    BOOL:     "n",
    TRISTATE: "n",
    HEX:      "",
    INT:      "",
    STRING:   "",
}

# Constant representing that there's no cached choice selection. This is
# distinct from a cached None (no selection). We create a unique object (any
# will do) for it so we can test with 'is'.
_NO_CACHED_SELECTION = object()

# Integers representing expression types
(
    _AND,
    _OR,
    _NOT,
    _EQUAL,
    _UNEQUAL,
    _LESS,
    _LESS_EQUAL,
    _GREATER,
    _GREATER_EQUAL,
) = range(9)

# Used in comparisons. 0 means the base is inferred from the format of the
# string. The entries for BOOL and TRISTATE are a convenience - they should
# never convert to valid numbers.
_TYPE_TO_BASE = {
    BOOL:     0,
    HEX:      16,
    INT:      10,
    STRING:   0,
    TRISTATE: 0,
    UNKNOWN:  0,
}

# Map from tristate values to integers
_TRI_TO_INT = {
    "n": 0,
    "m": 1,
    "y": 2,
}

_RELATIONS = frozenset((
    _EQUAL,
    _UNEQUAL,
    _LESS,
    _LESS_EQUAL,
    _GREATER,
    _GREATER_EQUAL,
))

# Token to relation (=, !=, <, ...) mapping
_TOKEN_TO_REL = {
    _T_EQUAL:         _EQUAL,
    _T_GREATER:       _GREATER,
    _T_GREATER_EQUAL: _GREATER_EQUAL,
    _T_LESS:          _LESS,
    _T_LESS_EQUAL:    _LESS_EQUAL,
    _T_UNEQUAL:       _UNEQUAL,
}

_RELATION_TO_STR = {
    _EQUAL:         "=",
    _GREATER:       ">",
    _GREATER_EQUAL: ">=",
    _LESS:          "<",
    _LESS_EQUAL:    "<=",
    _UNEQUAL:       "!=",
}
