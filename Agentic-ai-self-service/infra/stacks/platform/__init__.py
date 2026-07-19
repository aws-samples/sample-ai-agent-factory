"""Builder modules for the PlatformStack.

Each module exposes plain builder functions that take the stack as the
construct scope (plus explicit resource dependencies) and create constructs
with the SAME construct ids the original monolithic ``platform_stack.py``
used. This keeps every CloudFormation logical ID identical — the stack is
deployed live, and a new Construct subclass would insert a path segment and
change (i.e. REPLACE) every child resource.
"""
