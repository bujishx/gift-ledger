import assert from 'node:assert/strict';
import test from 'node:test';
import { revisionReview } from '../src/lib/ledgerRevision.js';

test('rebase updates untouched amount, keeps draft remark, and leaves inputs intact', () => {
  const source = { amount:'100.00', remark:'' };
  const latest = { amount:'200.00', remark:'' };
  const draft = { amount:'100', remark:'草稿' };
  const review = revisionReview(source, latest, draft, { amount:'礼金', remark:'备注' });
  assert.deepEqual(review.merged, { amount:'200.00', remark:'草稿' });
  assert.equal(draft.amount, '100');
  assert.match(review.changes[0], /200.00/);
});

test('two changed amounts are shown side by side for explicit user review', () => {
  const review = revisionReview({ amount:'100' }, { amount:'200' }, { amount:'150' }, {amount:'礼金'});
  assert.deepEqual(review.merged, {amount:'150'});
  assert.match(review.changes[0], /200.*150/);
});
