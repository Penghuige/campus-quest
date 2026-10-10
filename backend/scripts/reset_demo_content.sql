-- Remove the demo-seeded world (scoped: 【演示】 tasks + the 20 demo
-- students' points/notifications + demo reward items). FK-ordered, same
-- discipline as tests/e2e/factories.clean_world.
--
-- TARGET DATABASE: the demo/dev stack ONLY. The scoping patterns
-- ('【演示】%' titles, '202500%' student numbers) overlap test-factory
-- seeded rows on shared databases — running this against campusquest_test
-- would delete the e2e world's students' points.
BEGIN;

-- Community children first, then comments/ratings (scoped to demo tasks).
DELETE FROM comment_revisions WHERE comment_id IN (
  SELECT id FROM comments WHERE task_id IN (SELECT id FROM tasks WHERE title LIKE '【演示】%'));
DELETE FROM comment_votes WHERE comment_id IN (
  SELECT id FROM comments WHERE task_id IN (SELECT id FROM tasks WHERE title LIKE '【演示】%'));
DELETE FROM comment_reactions WHERE comment_id IN (
  SELECT id FROM comments WHERE task_id IN (SELECT id FROM tasks WHERE title LIKE '【演示】%'));
DELETE FROM comment_reports WHERE comment_id IN (
  SELECT id FROM comments WHERE task_id IN (SELECT id FROM tasks WHERE title LIKE '【演示】%'));
DELETE FROM comments WHERE task_id IN (SELECT id FROM tasks WHERE title LIKE '【演示】%');
DELETE FROM task_ratings WHERE task_id IN (SELECT id FROM tasks WHERE title LIKE '【演示】%');

-- Submission chain.
DELETE FROM upload_intents WHERE claim_id IN (
  SELECT id FROM assignment_claims WHERE task_id IN (SELECT id FROM tasks WHERE title LIKE '【演示】%'));
DELETE FROM submission_validations WHERE submission_id IN (
  SELECT s.id FROM submissions s JOIN assignment_claims c ON c.id = s.claim_id
  WHERE c.task_id IN (SELECT id FROM tasks WHERE title LIKE '【演示】%'));
DELETE FROM submission_reviews WHERE submission_id IN (
  SELECT s.id FROM submissions s JOIN assignment_claims c ON c.id = s.claim_id
  WHERE c.task_id IN (SELECT id FROM tasks WHERE title LIKE '【演示】%'));
DELETE FROM reward_lock_history WHERE claim_id IN (
  SELECT id FROM assignment_claims WHERE task_id IN (SELECT id FROM tasks WHERE title LIKE '【演示】%'));
DELETE FROM submissions WHERE claim_id IN (
  SELECT id FROM assignment_claims WHERE task_id IN (SELECT id FROM tasks WHERE title LIKE '【演示】%'));
DELETE FROM assignment_claims WHERE task_id IN (SELECT id FROM tasks WHERE title LIKE '【演示】%');

-- Task graph.
DELETE FROM task_collaborators WHERE task_id IN (SELECT id FROM tasks WHERE title LIKE '【演示】%');
DELETE FROM assignments WHERE task_id IN (SELECT id FROM tasks WHERE title LIKE '【演示】%');
DELETE FROM tasks WHERE title LIKE '【演示】%';

-- Points layer for the demo students (their only rows are demo rows).
DELETE FROM point_reservations WHERE user_id IN (SELECT id FROM users WHERE username LIKE '202500%');
DELETE FROM reward_redemptions WHERE user_id IN (SELECT id FROM users WHERE username LIKE '202500%');
DELETE FROM points_ledger WHERE user_id IN (SELECT id FROM users WHERE username LIKE '202500%');
DELETE FROM point_wallets WHERE user_id IN (SELECT id FROM users WHERE username LIKE '202500%');

-- Reward catalogue (demo items only; their redemptions are gone above).
DELETE FROM reward_items WHERE name LIKE '【演示】%';

-- Notifications for the demo students.
DELETE FROM notification_deliveries WHERE user_id IN (SELECT id FROM users WHERE username LIKE '202500%');
DELETE FROM notifications WHERE user_id IN (SELECT id FROM users WHERE username LIKE '202500%');

COMMIT;
