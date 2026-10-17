"""The genres of mutant, each a module naming what it does to a function.

A genre takes a function node and returns its mutants: each a replacement
(the statements that stand in for the body, or the node that stands in for
one of its nodes), the path of what it replaces within the function, and a
description for the report.
"""
