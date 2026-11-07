export function classify(value) {
  if (value > 0) {
    return "positive";
  }
  return "nonpositive";
}

export async function later(value) {
  await Promise.resolve();
  return value * 2;
}

export function unused() {
  return "never";
}
