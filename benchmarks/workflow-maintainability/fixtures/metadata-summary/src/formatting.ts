export const formatAge = (updatedAt: string | null, now: Date): string => {
  if (updatedAt === null) return 'unknown';
  const minutes = Math.floor(Math.max(0, now.getTime() - new Date(updatedAt).getTime()) / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h`;
  return `${Math.floor(minutes / 1440)}d`;
};

export const formatSize = (bytes: number | null): string => {
  if (bytes === null) return 'unknown';
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)}KiB`;
  return `${(bytes / 1024 ** 2).toFixed(1)}MiB`;
};
