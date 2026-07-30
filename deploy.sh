#!/usr/bin/env bash
# Build both maps and publish them to the gh-pages branch.
#
# Mirrors the gulf-islands layout: source lives on main, the rendered site is a
# separate orphan gh-pages branch holding index.html, region.html and their
# tile cache. GitHub Pages serves that branch's root.
#
#   ./deploy.sh            # rebuild from cache and deploy
#   ./deploy.sh --fetch    # refetch all layer data first (slow)
#
set -euo pipefail

SITE_DIR="site"
ISLAND_ZOOMS="9-14"
REGION_ZOOMS="9-13"
BRANCH="gh-pages"

RENDER="--render-only"
if [[ "${1:-}" == "--fetch" ]]; then
  RENDER=""
  echo "==> Full fetch: this hits ~20 external APIs and takes a while"
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

echo "==> Building island map (landing page)"
python3 lbi_map.py \
  --bbox lbi \
  --out "$SITE_DIR/index.html" \
  --cache-dir output/lbi-island \
  --title "Long Beach Island — Field Map" \
  --center-label "Ship Bottom" \
  --cache-tiles default --tile-zooms "$ISLAND_ZOOMS" \
  --habitat-rank 4 \
  --page-link "region.html|Full region & Pine Barrens →" \
  $RENDER

echo "==> Building regional map (Pine Barrens extent)"
python3 lbi_map.py \
  --bbox lbi-region \
  --out "$SITE_DIR/region.html" \
  --cache-dir output/lbi \
  --title "LBI Region — Barnegat Bay to the Pine Barrens" \
  --center-label "Ship Bottom" \
  --cache-tiles default --tile-zooms "$REGION_ZOOMS" --max-tiles 40000 \
  --habitat-rank 5 --skip sig_habitat \
  --page-link "index.html|← Island detail map" \
  $RENDER

# Pages would otherwise run the site through Jekyll, which ignores files and
# directories beginning with an underscore and adds nothing we want.
touch "$SITE_DIR/.nojekyll"

echo "==> Publishing $SITE_DIR to $BRANCH"
# Build the branch from a temporary index so the working tree is untouched.
tmp_index=$(mktemp -t lbi-deploy-index)
rm -f "$tmp_index"
export GIT_INDEX_FILE="$tmp_index"

git read-tree --empty
git add --force "$SITE_DIR"
tree=$(git write-tree --prefix="$SITE_DIR")

unset GIT_INDEX_FILE
rm -f "$tmp_index"

parent=$(git rev-parse --verify --quiet "refs/heads/$BRANCH" || true)
msg="Deploy: $(date '+%b %-d, %Y') — island and regional maps"
if [[ -n "$parent" ]]; then
  commit=$(git commit-tree "$tree" -p "$parent" -m "$msg")
else
  commit=$(git commit-tree "$tree" -m "$msg")
fi
git update-ref "refs/heads/$BRANCH" "$commit"

git push origin "$BRANCH"

echo
echo "==> Deployed"
echo "    https://marston-j.github.io/long-beach-island/"
echo "    https://marston-j.github.io/long-beach-island/region.html"
