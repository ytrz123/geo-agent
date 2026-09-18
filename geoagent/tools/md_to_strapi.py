#!/usr/bin/env python3
"""Convert Markdown article to Strapi-ready HTML payload.
Usage: python3 md_to_strapi.py <input.md>
Outputs JSON with title, slug, excerpt, content_html suitable for Strapi POST.
"""

import sys, json, re, markdown

def parse_frontmatter(text):
    """Extract --- frontmatter --- and markdown body."""
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', text, re.DOTALL)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).strip().split('\n'):
        if ':' in line:
            k, v = line.split(':', 1)
            meta[k.strip()] = v.strip()
    return meta, m.group(2).strip()

def extract_excerpt(html, chars=150):
    """Extract first paragraph as excerpt."""
    clean = re.sub(r'<[^>]+>', '', html)
    return clean[:chars] + ('...' if len(clean) > chars else '')

def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: md_to_strapi.py <file.md>"}))
        sys.exit(1)

    with open(sys.argv[1], 'r') as f:
        text = f.read()

    meta, body = parse_frontmatter(text)

    # Pre-process: extract <section data-faq> blocks so markdown inside them gets converted
    # Use unique placeholder that won't be parsed as markdown
    sections = {}
    def stash_section(m):
        key = f'[[SEC{len(sections)}]]'
        sections[key] = m.group(0)
        return f'\n\n{key}\n\n'
    body = re.sub(r'<section[^>]*>.*?</section>', stash_section, body, flags=re.DOTALL)

    # Convert markdown to HTML with tables + code blocks
    md = markdown.Markdown(extensions=['tables', 'fenced_code', 'codehilite'])
    html = md.convert(body)

    # Post-process: extract markdown inside stashed sections, convert, and restore
    for key, section_html in sections.items():
        inner_md = re.search(r'<section[^>]*>(.*?)</section>', section_html, re.DOTALL)
        if inner_md:
            converted_inner = md.convert(inner_md.group(1))
            restored = section_html.replace(inner_md.group(1), converted_inner)
            html = html.replace(f'<p>{key}</p>', restored)
            html = html.replace(key, restored)
        else:
            html = html.replace(f'<p>{key}</p>', section_html)
            html = html.replace(key, section_html)

    # Auto-generate slug if not in frontmatter
    title = meta.get('title', 'Untitled')
    slug = meta.get('slug', re.sub(r'[^a-z0-9]+', '-', title.lower().strip()).strip('-'))

    # Auto-detect title from first h1 if not in frontmatter
    if not title or title == 'Untitled':
        h1_match = re.search(r'<h1>(.*?)</h1>', html)
        if h1_match:
            title = h1_match.group(1)
            slug = re.sub(r'[^a-z0-9]+', '-', title.lower().strip()).strip('-')

    # Remove first H1 from content body (Strapi renders title as page heading, avoid duplicate)
    html = re.sub(r'<h1>.*?</h1>\s*', '', html, count=1)

    result = {
        "title": title,
        "slug": slug,
        "excerpt": meta.get('excerpt', extract_excerpt(html)),
        "category": meta.get('category', 'company'),
        "content": html,
        "keywords": meta.get('keywords', ''),
        "author": meta.get('author', ''),
        "geo_watermark": meta.get('geo_watermark', ''),
        "data_updated": meta.get('data_updated', ''),
        "char_count": len(body),
    }
    print(json.dumps(result, ensure_ascii=False))

if __name__ == '__main__':
    main()