/** 路径工具：快照库内 path 一律为 POSIX 风格相对路径，根目录表示为 '.' 或空。 */

/** 'a/b/c.txt' → 'a/b'；'c.txt' → ''（根目录）。 */
export function dirNameOf(path: string): string {
  const idx = path.lastIndexOf('/')
  return idx === -1 ? '' : path.slice(0, idx)
}

/** 'a/b/c.txt' → 'c.txt'。 */
export function baseNameOf(path: string): string {
  const idx = path.lastIndexOf('/')
  return idx === -1 ? path : path.slice(idx + 1)
}

/** 拼接当前目录下子项的完整路径：父路径为 '.' / 空 时即为根，直接返回 name。 */
export function joinChildPath(parentPath: string | null | undefined, name: string): string {
  if (!parentPath || parentPath === '.') return name
  return `${parentPath}/${name}`
}

/** 跳转到某快照浏览页并定位到 dirPath（空串 = 根目录）。 */
export function snapshotBrowseUrl(snapshotId: string, dirPath: string): string {
  const params = new URLSearchParams()
  params.set('snapshot', snapshotId)
  params.set('tab', 'browse')
  if (dirPath) params.set('open_path', dirPath)
  return `/snapshots?${params.toString()}`
}
