// WorkBuddy 送审包构建器（技能单包形态，YQ-1153；连接器因双运行时后置）
// 用法：node hosts/workbuddy/build.mjs
// 产物：dist/wb-skill/（技能包）
// 纪律：只做拷贝与组包，不改源文件；连跑两次产物逐字节一致（幂等）。
import { cpSync, rmSync, mkdirSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, '..', '..');
const DIST = path.join(ROOT, 'dist');

function countFiles(dir) {
  let n = 0;
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    if (e.isDirectory()) n += countFiles(path.join(dir, e.name));
    else n += 1;
  }
  return n;
}

const SKILL = path.join(DIST, 'wb-skill');
rmSync(SKILL, { recursive: true, force: true });
mkdirSync(path.join(SKILL, 'references'), { recursive: true });
mkdirSync(path.join(SKILL, 'templates'), { recursive: true });
cpSync(path.join(HERE, 'wb-skill', 'SKILL.md'), path.join(SKILL, 'SKILL.md'));
cpSync(path.join(ROOT, 'README.md'), path.join(SKILL, 'references', 'README.md'));
cpSync(path.join(ROOT, 'docs', 'FORMAT.md'), path.join(SKILL, 'references', 'FORMAT.md'));
cpSync(path.join(ROOT, 'packs', 'kbfold', 'rules.example.yaml'),
        path.join(SKILL, 'templates', 'rules.example.yaml'));
for (const f of ['file-kind.yaml', 'project-stage.yaml']) {
  cpSync(path.join(ROOT, 'packs', 'kbfold', 'templates', f),
          path.join(SKILL, 'templates', f));
}
cpSync(path.join(ROOT, 'LICENSE'), path.join(SKILL, 'LICENSE'));

console.log(`[build] dist/wb-skill: ${countFiles(SKILL)} 件（本包为技能单包形态，连接器后置）`);
