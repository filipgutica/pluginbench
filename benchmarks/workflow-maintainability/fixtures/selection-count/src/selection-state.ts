export class SelectionState {
  private paths = new Set<string>();

  has(path: string): boolean { return this.paths.has(path); }

  toggle(path: string): void {
    if (this.paths.has(path)) this.paths.delete(path);
    else this.paths.add(path);
  }

  replace(paths: Iterable<string>): void { this.paths = new Set(paths); }

  clear(): void { this.paths.clear(); }
}
