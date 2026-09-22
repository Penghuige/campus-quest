/**
 * Offset-page accumulation for "load more" lists (patterns §4/§9).
 *
 * The T2-T8 islands each grew the same inline merge when appending an
 * offset page: keep loaded rows, append only unseen ones, refresh the
 * server total. Extracted here (the T8 review fold: the semantics get a
 * unit pin instead of living untested inside five components) so the
 * comment/inbox threads and the teacher workbench lists share ONE
 * implementation.
 *
 * Why merge instead of blind concat: offset windows move under concurrent
 * writes (a new comment shifts rows right; a deleted one shifts them
 * left), so the next page can overlap the loaded prefix. Deduping by the
 * row's identity key keeps every row unique without re-sorting — the
 * SERVER's order is the product order (patterns §3: ordering is never a
 * client decision).
 *
 * `keyOf` is an explicit parameter (not a `{ id }` constraint): lists key
 * on different identity fields (comment `id`, submission `submission_id`),
 * and an explicit key keeps one total implementation instead of ad-hoc
 * wrappers.
 */

/**
 * Append one incoming offset page to the loaded rows, dropping rows whose
 * key is already present. Order: loaded rows first, then the unseen
 * incoming rows in server order. Neither input is mutated.
 */
export function mergeOffsetPage<T>(
  previous: readonly T[],
  incoming: readonly T[],
  keyOf: (row: T) => string,
): T[] {
  const seen = new Set(previous.map(keyOf));
  const fresh = incoming.filter((row) => !seen.has(keyOf(row)));
  return [...previous, ...fresh];
}

/** More pages exist on the server for an offset-accumulated list. */
export function hasMorePages(loadedCount: number, total: number): boolean {
  return loadedCount < total;
}
