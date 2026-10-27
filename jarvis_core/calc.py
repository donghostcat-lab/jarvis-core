"""Calculator: exact numbers come from fixed code, never from a model.

A small AST evaluator (no eval): numbers, + - * / // % **, parentheses and a few functions.
"""

import ast
import math
import operator

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"round": round, "abs": abs, "min": min, "max": max, "sum": lambda *a: sum(a), "sqrt": math.sqrt}


def calculate(expr: str) -> float:
    """Evaluates an arithmetic expression safely. Raises ValueError for anything else."""
    expr = expr.replace("×", "*").replace("÷", "/").replace("^", "**")
    if len(expr) > 300:
        raise ValueError("Expression too long")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("Exponent too large")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise ValueError("Only numbers, basic arithmetic, parentheses and round/abs/min/max/sum/sqrt.")

    return ev(ast.parse(expr, mode="eval"))


def format_number(x: float) -> str:
    """1,234.5 – at most 6 decimal places, without trailing zeros."""
    return f"{x:,.6f}".rstrip("0").rstrip(".") if isinstance(x, float) else f"{x:,}"
