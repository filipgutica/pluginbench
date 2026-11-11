/** Fence languages that render through the library's UiCodeBlock; other fences stay plain. */
export const libraryCodeLanguages = ['sh', 'yaml', 'json'];

/**
 * For each line of a shell sample, whether it continues the previous line's
 * command: after an unescaped trailing backslash, or inside a quote left open.
 * Those lines get no `$` prompt. A backslash in single quotes or in a comment
 * does not continue the command.
 */
export const continuesCommand = (code: string) => {
  let quote: "'" | '"' | undefined;
  let continues = false;
  return code.split('\n').map((line) => {
    const continued = continues;
    continues = false;
    for (let index = 0; index < line.length; index += 1) {
      const char = line[index];
      if (quote === "'") {
        if (char === "'") quote = undefined;
      } else if (char === '\\') {
        if (index === line.length - 1) continues = true;
        index += 1;
      } else if (quote === '"') {
        if (char === '"') quote = undefined;
      } else if (char === "'" || char === '"') {
        quote = char;
      } else if (char === '#' && (index === 0 || /\s/.test(line[index - 1]))) {
        break;
      }
    }
    if (quote) continues = true;
    return continued;
  });
};
