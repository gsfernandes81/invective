"""Mutation testing for a pytest suite: break one line, see whether a test fails.

`invective.mutate` is the engine, for one module against a chosen selection of
tests. `invective.sweep` runs it over every module of a source tree against
the test files that import each one.
"""
