/** Fence languages that render through the library's UiCodeBlock; other fences stay plain. */
export const libraryCodeLanguages = ['sh', 'yaml', 'json'];

/**
 * For each line of a shell sample, whether it continues the previous line's
 * command after a trailing backslash. Those lines get no `$` prompt.
 */
export const continuesCommand = (code: string) =>
  code.split('\n').map((_, index, lines) => index > 0 && lines[index - 1].endsWith('\\'));
