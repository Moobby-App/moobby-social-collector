// Local adapter for Tweet Harvest 2.7.1. Original package remains unchanged.
function selectText(result) {
  const tweet = result.legacy ? result : (result.tweet || result);
  const legacy = tweet.legacy || {};
  const candidate = tweet.note_tweet?.note_tweet_results?.result?.text;
  const hasNote = typeof candidate === 'string' && candidate.trim().length > 0;
  const extended = legacy.extended_entities?.media;
  const basic = legacy.entities?.media;
  const items = Array.isArray(extended) ? extended : (Array.isArray(basic) ? basic : []);
  const basis = Array.isArray(extended) ? 'extended_entities' : (Array.isArray(basic) ? 'entities' : 'not_available');
  const seen = new Set();
  const media = [];
  for (const [index, item] of items.entries()) {
    const url = item.media_url_https || null;
    const key = item.media_key || item.id_str || url;
    if (!key || seen.has(key)) continue;
    seen.add(key);
    media.push({
      media_id: item.id_str || null,
      media_key: item.media_key || null,
      media_index: index + 1,
      type: item.type || 'unknown',
      // For video/GIF this URL points to a thumbnail, not a photo attachment.
      image_url: item.type === 'photo' ? url : null,
      thumbnail_url: item.type === 'photo' ? null : url,
      alt_text: item.ext_alt_text || null,
      width: item.original_info?.width || null,
      height: item.original_info?.height || null,
    });
  }
  const images = media.filter(item => item.type === 'photo' && item.image_url);
  return {
    ...legacy,
    post_type: legacy.in_reply_to_status_id_str ? 'reply' : (legacy.retweeted_status_result || tweet.retweeted_status_result ? 'repost' : (legacy.is_quote_status || tweet.quoted_status_result ? 'quote' : 'original')),
    full_text: hasNote ? candidate : legacy.full_text,
    legacy_full_text: legacy.full_text || '',
    text_extraction_method: hasNote ? 'note_tweet' : 'legacy.full_text',
    // Field presence alone is not an independent completeness verification.
    text_completeness: legacy.truncated === true && !hasNote ? 'truncated' : 'unknown',
    media_json: JSON.stringify(media),
    image_urls_json: JSON.stringify(images.map(item => item.image_url)),
    media_count: media.length,
    image_count: images.length,
    media_extraction_basis: basis,
  };
}
module.exports = {selectText};
