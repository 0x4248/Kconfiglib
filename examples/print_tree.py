# Prints the menu tree of the configuration. Dependencies between symbols can
# sometimes implicitly alter the menu structure (see kconfig-language.txt), and
# that's implemented too.
#
# Usage:
#
#   $ make [ARCH=<arch>] scriptconfig SCRIPT=Kconfiglib/examples/print_tree.py
#
# Example output:
#
#   ...
#   config HAVE_KERNEL_LZO
#   config HAVE_KERNEL_LZ4
#   choice
#     config KERNEL_GZIP
#     config KERNEL_BZIP2
#     config KERNEL_LZMA
#     config KERNEL_XZ
#     config KERNEL_LZO
#     config KERNEL_LZ4
#   config DEFAULT_HOSTNAME
#   config SWAP
#   config SYSVIPC
#     config SYSVIPC_SYSCTL
#   config POSIX_MQUEUE
#     config POSIX_MQUEUE_SYSCTL
#   config CROSS_MEMORY_ATTACH
#   config FHANDLE
#   config USELIB
#   config AUDIT
#   config HAVE_ARCH_AUDITSYSCALL
#   config AUDITSYSCALL
#   config AUDIT_WATCH
#   config AUDIT_TREE
#   menu "IRQ subsystem"
#     config MAY_HAVE_SPARSE_IRQ
#     config GENERIC_IRQ_LEGACY
#     config GENERIC_IRQ_PROBE
#   ...

from kconfiglib import Config, Symbol, Choice, MENU, COMMENT
import sys

def indent_print(s, indent):
    print((" " * indent) + s)

def print_items(node, indent):
    while node is not None:
        if isinstance(node.item, Symbol):
            indent_print("config " + node.item.name, indent)

        elif isinstance(node.item, Choice):
            indent_print("choice", indent)

        elif node.item == MENU:
            indent_print('menu "{0}"'.format(node.prompt[0]), indent)

        elif node.item == COMMENT:
            indent_print('comment "{0}"'.format(node.prompt[0]), indent)

        if node.list is not None:
            print_items(node.list, indent + 2)

        node = node.next

conf = Config(sys.argv[1])
print_items(conf.top_menu, 0)
