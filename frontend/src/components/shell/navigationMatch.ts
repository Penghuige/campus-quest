/** Match path segments; independent workspaces can exclude nested subtrees. */
export function navItemActive(pathname: string, href: string, exclude: readonly string[] = []): boolean {
  const within = (prefix: string) => pathname === prefix || pathname.startsWith(`${prefix}/`);
  return (href === "/" ? pathname === "/" : within(href)) && !exclude.some(within);
}
