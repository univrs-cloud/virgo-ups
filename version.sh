#!/bin/bash

set -euo pipefail

package=virgo-ups
changelog=debian/changelog

usage() {
  cat <<EOF
Usage: ./version.sh <major|minor|patch|X.Y.Z> [-m "changelog entry"]...

Writes a new $changelog stanza, commits it and tags it v<version>.

  ./version.sh patch -m "Fix battery capacity read"
  ./version.sh 2.2.0 -m "Add charge control" -m "Drop python3-pip"

Push the result with: git push --follow-tags
EOF
}

[ $# -ge 1 ] || { usage; exit 1; }

bump=$1
shift

entries=()
while [ $# -gt 0 ]; do
  case $1 in
    -m|--message)
      [ $# -ge 2 ] || { echo "error: $1 needs a value" >&2; exit 1; }
      entries+=("$2")
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unexpected argument '$1'" >&2; usage; exit 1 ;;
  esac
done

cd "$(dirname "$0")"

[ -f $changelog ] || { echo "error: $changelog not found" >&2; exit 1; }

if [ -n "$(git status --porcelain)" ]; then
  echo "error: working tree is not clean" >&2
  exit 1
fi

current=$(sed -n "1s/^$package (\([^)]*\)).*/\1/p" $changelog)
[ -n "$current" ] || { echo "error: cannot read the current version from $changelog" >&2; exit 1; }

IFS=. read -r major minor patch <<<"$current"

case $bump in
  major) version="$((major + 1)).0.0" ;;
  minor) version="$major.$((minor + 1)).0" ;;
  patch) version="$major.$minor.$((patch + 1))" ;;
  v*) version=${bump#v} ;;
  *) version=$bump ;;
esac

if ! [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "error: '$bump' is not major, minor, patch or an X.Y.Z version" >&2
  exit 1
fi

if [ "$(printf '%s\n%s\n' "$current" "$version" | sort -V | tail -1)" != "$version" ]; then
  echo "error: $version is not newer than $current" >&2
  exit 1
fi

if git rev-parse -q --verify "refs/tags/v$version" >/dev/null; then
  echo "error: tag v$version already exists" >&2
  exit 1
fi

[ ${#entries[@]} -gt 0 ] || entries=("Release $version")

author="$(git config user.name) <$(git config user.email)>"

{
  echo "$package ($version) stable; urgency=medium"
  for entry in "${entries[@]}"; do
    echo "  * $entry"
  done
  echo " -- $author  $(date -u -R)"
  echo
  cat $changelog
} > $changelog.new
mv $changelog.new $changelog

git add $changelog
git commit -m "$version" --quiet
git tag -a "v$version" -m "$version"

echo "$package $version tagged as v$version"
echo "push it with: git push --follow-tags"
