# Podcast covers

One square cover per source, named in its `sources.toml` entry (`cover`). After each publish the runner looks at the podcast in Audiobookshelf, and if it has no cover yet (as when the first upload has just created it) it uploads this one. A cover already set is never replaced.

Each cover is a square adaptation of the publication's logo, 3000 px, between the 1400 and 3000 px podcast apps ask for:

- **`new-atlantis.png`**: The New Atlantis's wordmark from the site's header ("THE NEW" over "ATLANTIS", the two SVGs in the navbar at their own proportions), white on the blue of the site's "NA" icon, `#034E90`. `new-atlantis.svg` is its source; render it with `rsvg-convert -w 3000 -h 3000 new-atlantis.svg -o new-atlantis.png`.

A new source needs its cover here before its first night.
