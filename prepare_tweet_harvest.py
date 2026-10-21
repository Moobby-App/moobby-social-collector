"""Generate a local entry point; fail if the pinned upstream layout changes."""
import json
from pathlib import Path


def prepare_entry(experiment):
    original = experiment / 'node_modules/tweet-harvest/dist/bin.js'
    target = original.with_name('moobby-bin.js')
    helper = experiment / 'text_extraction.cjs'
    source = original.read_text(encoding='utf-8')
    replacements = {
        'const tweetContent = result.legacy || result.tweet.legacy;':
            'const tweetContent = require(' + json.dumps(str(helper)) + ').selectText(result);',
        'tweet["full_text"] = cleanTweetText;':
            'tweet["full_text"] = cleanTweetText;\n'
            '            for (const key of ["post_type", "legacy_full_text", "text_extraction_method", "text_completeness", "media_json", "image_urls_json", "media_count", "image_count", "media_extraction_basis"]) tweet[key] = current.tweet[key];',
        'const options = program.opts();':
            'const options = program.opts();\n'
            '  if (process.env.MOOBBY_X_SESSION_FILE) {\n'
            '    const state = JSON.parse(require("fs").readFileSync(process.env.MOOBBY_X_SESSION_FILE, "utf8"));\n'
            '    const cookie = (state.cookies || []).find(c => c.name === "auth_token" && ["x.com", ".x.com"].includes(c.domain) && (c.expires === -1 || c.expires > Date.now()/1000));\n'
            '    if (!cookie) { console.error("Session X tidak tersedia atau kedaluwarsa."); process.exit(1); }\n'
            '    options.token = cookie.value;\n'
            '  }',
    }
    for old, new in replacements.items():
        if source.count(old) != 1:
            raise ValueError('Struktur Tweet Harvest berubah; adapter tidak diterapkan.')
        source = source.replace(old, new)
    if not helper.is_file():
        raise ValueError('Adapter teks belum tersedia.')
    target.write_text(source, encoding='utf-8')
    return target
