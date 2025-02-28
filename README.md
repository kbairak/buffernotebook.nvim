# BufferNotebook.nvim

Turn any neovim buffer into a python notebook.

## Installation

### Requirements

You need to have [pynvim](https://pynvim.readthedocs.io) installed in your
system globally.

### Manual installation

- Copy/merge the rplugin directory inside your neovim configuration folder.
- Run `:UpdateRemotePlugins`
- Restart neovim

### Using [lazy.nvim](https://lazy.folke.io)

```lua
{
  "kbairak/bufferformula.nvim",
  build = ":UpdateRemotePlugins",
}
```

### Recommended configuration

```lua
{
  "kbairak/buffernotebook.nvim",
  build = ":UpdateRemotePlugins",
  cmd = "BufferNotebook",  -- For lazy loading
  init = function()
    vim.keymap.set("n", "<Leader>bn", ":BufferNotebook toggle<CR>")
    vim.keymap.set("n", "<Leader>bN", ":BufferNotebook inject<CR>")
  end,
}
```

## Usage

Start/stop the plugin per buffer with `:BufferNotebook enable`,
`:BufferNotebook disable`, `:BufferNotebook toggle`.

After that, each line ending with `#=`, assuming the computation can be
performed, will show the result.

- `1  #=` will become `1  #= 1`
- `1 + 2  #=` will become `1 + 2  #= 3`
- etc

Another way is to add this line `# <<<` after a statement. So this

```python
1 + 2
# <<<
```

will become

```python
1 + 2
# <<< 3
```

These changes are not actually part of the buffer. They are displayed like LSP
messages.

You can assign values or computations to variables and they will be remembered:

- ```python
  a = 1
  a + 1  #=
  ```

  will become

  ```python
  a = 1
  a + 1  #= 2
  ```

  Changes are applied in real-time. So in the previous example, if you change
  the first line from `a = 1` to `a = 2`, then the result will immediately
  change from `a + 1  #= 2` to `a + 1  #= 3` immediately.

- And you can import modules or define functions and use them:
  
  ```python
  import ast
  
  ast.dump(ast.parse('a = 1'))
  # <<< Module(body=[Assign(targets=[Name(id='a', ctx=Store())], value=Constant(value=1))])
  ```

  ```python
  import re

  re.search(r'^(\d\d):(\d\d):(\d\d)$', '12:33:48').groups()
  # <<< ('12', '33', '48')
  ```

- Another way to use this is the `:BufferNotebook inject` command (which you
  should bind to a keymap). This will actually inject the output of the
  statement the cursor is over in the buffer. This is especially useful if you
  want to copy the result and paste it somewhere else or if the output is so
  big it doesn't fit the window well.

  - For example, running `inject` on this:

    ```python
    ['hello ' * 4] * 4
    ```

    will render this:

    ```python
    ['hello ' * 4] * 4
    # <<< ['hello hello hello hello ',
    # ...  'hello hello hello hello ',
    # ...  'hello hello hello hello ',
    # ...  'hello hello hello hello ']
    ```

  - and this:

    ```python
    "\n".join(['hello ' * 4] * 4)

    will render this:

    ```python
    "\n".join(['hello ' * 4] * 4)
    # <<< hello hello hello hello 
    # ... hello hello hello hello 
    # ... hello hello hello hello 
    # ... hello hello hello hello 
    ```

## How it works

- Evaluations are performed on a delay to prevent triggering many potentially
  expensive computations when heavily editing a buffer.

- The first step is to only keep the lines of the buffer that can actually be
  parsed by python. So this

  ```python
  a = 1 
  This is a line that cannot possible be parsed by python
  a + 2  #=
  ```

  will be parsed as

  ```python
  a = 1 
  
  a + 2  #=
  ```

- The next step is to keep track of which lines contain a `#=` or `# <<<` mark
  and which statements they correspond to

- Next, the top-level statements are executed in order.

  - A cache is used to avoid doing unnecessary computations and invalidating
    experiments that depend on randomness. Editing a line will trigger this
    line's reevaluation and all lines after that. The statements that came
    before however, will remain unaffected. So, if you have:

    ```python
    import random
    a = 1
    b = random.random()
    c = 2
    b + 1  #= 1.939389315114433
    ```

    and you edit the `c = 2` line, `b` (and thus `b + 1`) will remain
    unaffected. If you edit the `a = 1` line however, `b` will be reevaluated
    and have a different value.

  - The cache key being used is `ast.dump(statement_ast)` so a line edited to do
    the exact same thing will not invalidate the cache (ie `a = 1000` and
    `a =  1_000` will be considered equivalent)

- A statement that was just evaluated and that has a `# =` or `# <<<` mark
  associated with it will annotate the buffer with the result of that statement.

  - Assignments will annotate the value that was just assigned
  - Expressions will annotate the evaluated value
  - Statements that raise an exception will annotate the exception prefixed by a
    `!`
  - Other statements (for example `import re  #=`) will not annotate anything

## Examples of usage

The most use I have made out of this plugin is learning how a library or tool
works by tweaking simple use cases in real time.

### Regular expression debugging

<https://github.com/user-attachments/assets/9ada4f4e-23c2-4cb4-bf7b-371d0f72eae6>

### Understanding how `lxml` works

<https://github.com/user-attachments/assets/b86b540c-f2a6-4637-9650-11df22ddaf20>
