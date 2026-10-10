/** Full-document navigation must use the app mount, independent of API base. */
export function resolveAppPath(path: string): string {
  return `${process.env.NEXT_PUBLIC_APP_BASE_PATH ?? ""}${path}`;
}
