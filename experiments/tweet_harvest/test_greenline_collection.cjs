const assert = require('node:assert/strict');
const {normalize, candidates} = require('./collect_greenline.cjs');
const start = Date.parse('2026-10-20T10:00:00Z');
const end = Date.parse('2026-10-21T10:00:00Z');
const tweet = {
  rest_id: '2112134311579406594',
  core: {user_results: {result: {core: {screen_name: 'penumpang', name: 'Penumpang'}}}},
  legacy: {id_str: '2112134311579406594', full_text: 'KRL Serpong tertahan',
    created_at: 'Wed Oct 21 09:00:00 +0000 2026', in_reply_to_status_id_str: '12',
    in_reply_to_screen_name: 'commuterline', conversation_id_str: '11',
    entities: {urls: [{expanded_url: 'https://example.com/report'}]}, extended_entities: {media: []}},
};
const post = normalize(tweet, start, end);
assert.equal(post.id_str, '2112134311579406594');
assert.equal(post.post_type, 'reply');
assert.equal(post.conversation_id, '11');
assert.equal(post.in_reply_to_status_id, '12');
assert.deepEqual(post.related_urls, ['https://example.com/report']);
assert.equal(normalize(tweet, start, Date.parse(post.published_at)), null);
assert.equal(normalize({...tweet, legacy: {...tweet.legacy, created_at: 'invalid'}}, start, end), null);
assert.equal(normalize({...tweet, legacy: {...tweet.legacy, created_at: 'Tue Oct 20 09:00:00 +0000 2026'}}, start, end), null);
assert.equal(candidates({instructions: [{tweet_results: {result: tweet}}]}).length, 1);
console.log('Green Line timestamp, reply context, large ID, and response traversal checks passed.');
