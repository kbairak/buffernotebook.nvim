import ast
import re
import threading

import pynvim
import pynvim.api

# TODOs:
# - [✅] One command with one argument for everything
# - [✅] Render complex assigns  (eg `a, b = 1, 2`)
# - [✅] Command/mapping to clear cache and re-evaluate
# - [ ] Command/mapping to save results in buffer
# - [ ] Figure something out for multiline


class BufferNotebook:
    def __init__(self, nvim: pynvim.Nvim, buffer: pynvim.api.Buffer):
        self.nvim = nvim
        self.buffer = buffer

        self.enabled = False
        self.namespace = self.nvim.api.create_namespace("BufferNotebookNamepsace")

        self.globals = {"__name__": "__main__"}
        self.cache = []
        self.timer = None

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
        if self.timer is not None:
            self.timer.cancel()
        self.timer = threading.Timer(0.3, lambda: self.nvim.async_call(self._on_change))
        self.timer.start()

    def _on_change(self):
        self.timer = None
        self.clear()

        lines = self.nvim.api.buf_get_lines(self.buffer, 0, -1, False)

        tree = self.parse()

        top_level_statements: list[tuple[int, int, ast.stmt]] = [
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
            elif re.search(r"^#\s<<<\s*$", line):
                fullline_marks.add(i)

        for i, (start_line_number, end_line_number, statement) in enumerate(
            top_level_statements
        ):
            key = ast.dump(statement)
            try:
                cache_key, cache_result = self.cache[i]
            except IndexError:
                result = self.run_statement(statement)
                self.cache.append((key, result))
            else:
                if key == cache_key:
                    result = cache_result
                else:
                    self.cache = self.cache[:i]
                    result = self.run_statement(statement)
                    self.cache.append((key, result))

            for line_number in (
                set(range(start_line_number, end_line_number + 1)) & inline_marks
            ):
                self.echo(line_number, result)

            try:
                (next_start_line_number, *_) = top_level_statements[i + 1]
            except IndexError:
                for line_number in fullline_marks:
                    if line_number > end_line_number:
                        self.echo(line_number, result)
            else:
                for line_number in (
                    set(range(end_line_number + 1, next_start_line_number))
                    & fullline_marks
                ):
                    self.echo(line_number, result)

    def parse(self) -> ast.Module:
        lines = self.nvim.api.buf_get_lines(self.buffer, 0, -1, False)
        return ast.parse("\n".join(self._parse(lines)))

    def _parse(self, lines: list[str]) -> list[str]:
        """Assuming most lines are ok, try to replacing as few lines as possible with empty ones so
        that the end result is parse-able.
        """

        if not lines:
            return []  # Recursion exit

        end = len(lines)
        while end > 0:
            try:
                # Bad:  [ G   G   G   B   G   G ]
                # Bad:  [ G   G   G   B   G ] G
                # Bad:  [ G   G   G   B ] G   G
                # Good: [ G   G   G ] B   G   G
                ast.parse("\n".join(lines[:end]))
            except Exception:
                end -= 1
            else:
                # Good:   [G G G]           B G G
                # Return: [G G G] + _parse([B G G])
                return lines[:end] + self._parse(lines[end:])

        # Ended the for-loop without encountering a good chunk; (at least) first line must be bad
        #          B             B             G G ...
        # Return: [""] + _parse([B             G G ...])
        # Return: [""] +        [""] + _parse([G G ...])
        return [""] + self._parse(lines[1:])

    def run_statement(self, statement: ast.stmt) -> str:
        if isinstance(statement, ast.Assign):
            try:
                exec(
                    compile(
                        ast.Module(body=[statement], type_ignores=[]),
                        "<string>",
                        "exec",
                    ),
                    self.globals,
                )
            except Exception as exc:
                return f"! {exc!r}"

            if len(statement.targets) != 1:
                return ""
            if isinstance(statement.targets[0], ast.Tuple) and all(
                isinstance(elt, ast.Name) for elt in statement.targets[0].elts
            ):
                result = []
                for elt in statement.targets[0].elts:
                    assert isinstance(elt, ast.Name)
                    elt.ctx = ast.Load()
                    result.append(
                        eval(
                            compile(ast.Expression(elt), "<string>", "eval"),
                            self.globals,
                        )
                    )
                return repr(tuple(result))
            elif isinstance(statement.targets[0], ast.Name):
                return repr(self.globals[statement.targets[0].id])
            else:
                return ""

        elif isinstance(statement, ast.AugAssign) and isinstance(
            statement.target, ast.Name
        ):
            try:
                exec(
                    compile(
                        ast.Module(body=[statement], type_ignores=[]),
                        "<string>",
                        "exec",
                    ),
                    self.globals,
                )
            except Exception as exc:
                return f"! {exc!r}"
            return repr(self.globals[statement.target.id])

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
                    compile(
                        ast.Module(body=[statement], type_ignores=[]),
                        "<string>",
                        "exec",
                    ),
                    self.globals,
                )
            except Exception:
                pass
            return ""

    def echo(self, line_number: int, text: str):
        self.nvim.api.buf_set_virtual_text(
            self.buffer, self.namespace, line_number, [(text, "Info")], {}
        )

    def reset(self):
        self.globals = {"__name__": "__main__"}
        self.cache = []
        self._on_change()


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

    @pynvim.autocmd("TextChanged,TextChangedI", pattern="*")
    def on_change(self, *_):
        self.get_notebook().on_change()

    @pynvim.autocmd("BufDelete", pattern="*")
    def on_buffer_delete(self, *_):
        buffer = self.nvim.current.buffer
        del self.notebooks[buffer.number]

    @pynvim.command(
        "BufferNotebook", nargs=1, complete="customlist,BufferNotebookCompletions"
    )
    def command(self, args: str):
        (subcommand,) = args
        if subcommand == "enable":
            self.get_notebook().enable()
        elif subcommand == "disable":
            self.get_notebook().disable()
        elif subcommand == "toggle":
            self.get_notebook().toggle()
        elif subcommand == "reset":
            self.get_notebook().reset()
        else:  # pragma: no cover
            raise Exception("Unreachable code")

    @pynvim.function("BufferNotebookCompletions", sync=True)
    def get_completions(self, *_):
        return ["enable", "disable", "toggle", "reset"]
