import ast
import re

import pynvim
import pynvim.api


class BufferNotebook:
    def __init__(self, nvim: pynvim.Nvim, buffer: pynvim.api.Buffer):
        self.nvim = nvim
        self.buffer = buffer

        self.enabled = False
        self.namespace = self.nvim.api.create_namespace("BufferNotebookNamepsace")

    def enable(self):
        self.enabled = True
        self.on_change()

    def disable(self):
        self.enabled = False
        self.clear()

    def toggle(self):
        if self.enabled:
            self.disable()
        else:
            self.enable()

    def clear(self):
        self.nvim.api.buf_clear_namespace(self.buffer, self.namespace, 0, -1)

    def on_change(self):
        if not self.enabled:
            return
        self.clear()

        lines = self.nvim.api.buf_get_lines(self.buffer, 0, -1, False)

        tree = self.parse()

        top_level_statements = [
            (
                statement.lineno - 1,
                (statement.end_lineno or statement.lineno) - 1,
                statement,
            )
            for statement in tree.body
        ]

        inline_marks, fullline_marks = set(), set()
        for i, line in enumerate(lines):
            if re.search(r"#=\s*$", line):
                inline_marks.add(i)
            elif re.search(r"^# <<<\s*$", line):
                fullline_marks.add(i)

        self.globals = {"__name__": "__main__"}
        for i, (start_line_number, end_line_number, statement) in enumerate(
            top_level_statements
        ):
            result = self.run_statement(statement)

            for line_number in (
                set(range(start_line_number, end_line_number + 1)) & inline_marks
            ):
                self.echo(line_number, result)

            if i < len(top_level_statements) - 1:
                (next_start_line_number, *_) = top_level_statements[i + 1]
                for line_number in (
                    set(range(end_line_number + 1, next_start_line_number))
                    & fullline_marks
                ):
                    self.echo(line_number, result)
            else:
                for line_number in fullline_marks:
                    if line_number <= end_line_number:
                        continue
                    self.echo(line_number, result)

    def parse(self) -> ast.Module:
        lines = self.nvim.api.buf_get_lines(self.buffer, 0, -1, False)
        return ast.parse("\n".join(self._parse(lines)))

    def _parse(self, lines: list[str]) -> list[str]:
        if not lines:
            return []
        stop = len(lines)
        while stop > 0:
            try:
                ast.parse("\n".join(lines[:stop]))
            except Exception:
                stop -= 1
            else:
                return lines[:stop] + self._parse(lines[stop:])
        return [""] + self._parse(lines[1:])

    def run_statement(self, statement: ast.AST) -> str:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            try:
                exec(
                    compile(ast.Module(body=[statement]), "<string>", "exec"),
                    self.globals,
                )
            except Exception as exc:
                return f"! {exc!r}"
            return repr(self.globals[statement.targets[0].id])
        elif isinstance(statement, ast.Expr):
            try:
                return repr(
                    eval(
                        compile(ast.Expression(statement.value), "<string>", "eval"),
                        self.globals,
                    )
                )
            except Exception as exc:
                return f"! {exc!r}"
        else:
            try:
                exec(
                    compile(ast.Module(body=[statement]), "<string>", "exec"),
                    self.globals,
                )
            except Exception:
                pass
            return "None"

    def echo(self, line_number: int, text: str):
        self.nvim.api.buf_set_virtual_text(
            self.buffer, self.namespace, line_number, [(text, "Info")], {}
        )


@pynvim.plugin
class BufferNotebookPlugin:
    def __init__(self, nvim: pynvim.Nvim):
        self.nvim = nvim
        self.notebooks = {}

    def get_notebook(self) -> BufferNotebook:
        buffer = self.nvim.current.buffer
        if buffer.number not in self.notebooks:
            self.notebooks[buffer.number] = BufferNotebook(self.nvim, buffer)
        return self.notebooks[buffer.number]

    @pynvim.command("BufferNotebookEnable", nargs="0", range="")
    def enable(self, *_):
        self.get_notebook().enable()

    @pynvim.command("BufferNotebookDisable", nargs="0", range="")
    def disable(self, *_):
        self.get_notebook().disable()

    @pynvim.command("BufferNotebookToggle", nargs="0", range="")
    def toggle(self, *_):
        self.get_notebook().toggle()

    @pynvim.autocmd("TextChanged,TextChangedI", pattern="*")
    def on_change(self, *_):
        self.get_notebook().on_change()
