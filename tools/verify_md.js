#!/usr/bin/env node
/* Markdown renderer checks. Run: node tools/verify_md.js */
'use strict';

const path = require('path');
const md = require(path.join(__dirname, '..', 'kiro_openai', 'static', 'md.js'));

let failed = [];

function check(label, condition, detail) {
  console.log(`[${condition ? 'PASS' : 'FAIL'}] ${label}` +
    (condition ? '' : `\n       -> ${detail}`));
  if (!condition) failed.push(label);
}

const has = (label, src, needle) => {
  const html = md.render(src);
  check(label, html.includes(needle), `got: ${html.slice(0, 260)}`);
};

console.log('== headings ==');
has('h1', '# Title', '<h1>Title</h1>');
has('h3', '### Deep', '<h3>Deep</h3>');
has('h6', '###### Six', '<h6>Six</h6>');
has('closing hashes trimmed', '## Mid ##', '<h2>Mid</h2>');

console.log('\n== inline ==');
has('bold', 'a **b** c', '<strong>b</strong>');
has('italic asterisk', 'a *b* c', '<em>b</em>');
has('italic underscore', 'a _b_ c', '<em>b</em>');
has('bold italic', '***x***', '<strong><em>x</em></strong>');
has('strikethrough', '~~gone~~', '<del>gone</del>');
has('inline code', 'use `npm i` now', '<code>npm i</code>');
has('snake_case survives', 'my_var_name here', 'my_var_name');
check('no stray em in snake_case', !md.render('my_var_name here').includes('<em>'),
  md.render('my_var_name here'));

console.log('\n== links ==');
has('link', '[site](https://example.com)', 'href="https://example.com"');
has('link opens safely', '[s](https://e.com)', 'rel="noopener noreferrer"');
has('autolink', 'see https://example.com now', '<a href="https://example.com"');
has('image', '![alt](https://e.com/i.png)', '<img src="https://e.com/i.png" alt="alt"');
check('javascript: url refused',
  !md.render('[x](javascript:alert(1))').includes('javascript'),
  md.render('[x](javascript:alert(1))'));
check('data: url refused',
  !md.render('[x](data:text/html;base64,PHN2Zz4=)').includes('data:'),
  md.render('[x](data:text/html;base64,PHN2Zz4=)'));

console.log('\n== escaping ==');
check('html escaped', md.render('<script>alert(1)</script>').includes('&lt;script&gt;'),
  md.render('<script>alert(1)</script>'));
check('no live script tag', !md.render('<script>x</script>').includes('<script>'), 'leaked');
check('code span contents escaped',
  md.render('`<b>hi</b>`').includes('<code>&lt;b&gt;hi&lt;/b&gt;</code>'),
  md.render('`<b>hi</b>`'));

console.log('\n== fenced code ==');
const code = md.render('```python\nprint("hi")\n```');
check('language captured', code.includes('<span class="lang">python</span>'), code);
check('copy button present', code.includes('class="code-copy"'), code);
check('raw source carried', code.includes(`data-raw="${encodeURIComponent('print("hi")')}"`), code);
check('body escaped', code.includes('print(&quot;hi&quot;)'), code);
check('untagged fence defaults to text',
  md.render('```\nplain\n```').includes('<span class="lang">text</span>'), 'no default');
check('unclosed fence marked streaming',
  md.render('```js\nlet a = 1').includes('class="code streaming"'), 'not marked');
check('tildes supported', md.render('~~~\nx\n~~~').includes('<figure class="code"'), 'no tilde fence');
check('markdown inside code is literal',
  md.render('```\n**not bold**\n```').includes('**not bold**'), 'was parsed');

console.log('\n== tables ==');
const tbl = md.render(`| Model | Cost |\n| --- | ---: |\n| auto | $0.04 |\n| opus | $0.09 |`);
check('table element', tbl.includes('<table class="md">'), tbl);
check('header cell', tbl.includes('<th>Model</th>'), tbl);
check('right alignment', tbl.includes('style="text-align:right"'), tbl);
check('two body rows', (tbl.match(/<tr>/g) || []).length === 3, tbl);
check('horizontally scrollable', tbl.includes('table-scroll'), tbl);
const centred = md.render(`| a |\n| :-: |\n| b |`);
check('centre alignment', centred.includes('text-align:center'), centred);
const noPipes = md.render(`Model | Cost\n--- | ---\nauto | $1`);
check('table without outer pipes', noPipes.includes('<th>Model</th>'), noPipes);
check('inline formatting inside cells',
  md.render('| a |\n| --- |\n| **b** |').includes('<strong>b</strong>'), 'not formatted');

console.log('\n== lists ==');
has('unordered', '- one\n- two', '<ul><li>one</li><li>two</li></ul>');
has('ordered', '1. one\n2. two', '<ol><li>one</li>');
has('plus bullets', '+ x', '<ul><li>x</li></ul>');
const nested = md.render('- a\n  - b\n- c');
check('nested list', nested.includes('<li>a<ul><li>b</li></ul></li>'), nested);
const task = md.render('- [x] done\n- [ ] todo');
check('checked task', task.includes('checked'), task);
check('unchecked task', task.includes('<input type="checkbox" disabled>'), task);
check('tasks are read-only', task.includes('disabled'), task);
check('wrapped item joined',
  md.render('- first line\n  continued').includes('first line\ncontinued') ||
  md.render('- first line\n  continued').includes('first line continued') ||
  md.render('- first line\n  continued').includes('continued'),
  md.render('- first line\n  continued'));

console.log('\n== quotes and rules ==');
has('blockquote', '> quoted', '<blockquote><p>quoted</p></blockquote>');
has('hr dashes', '---', '<hr>');
has('hr asterisks', '***', '<hr>');
check('quote can contain a list',
  md.render('> - a\n> - b').includes('<blockquote><ul>'), md.render('> - a\n> - b'));

console.log('\n== paragraphs ==');
has('paragraph', 'hello world', '<p>hello world</p>');
check('two paragraphs', (md.render('a\n\nb').match(/<p>/g) || []).length === 2, md.render('a\n\nb'));
has('soft break becomes br', 'a\nb', 'a<br>b');
check('no empty paragraphs', !md.render('a\n\n\n\nb').includes('<p></p>'), 'empty p');

console.log('\n== thinking ==');
let t = md.splitThinking('<thinking>weighing options</thinking>Final answer');
check('thinking captured', t.thinking === 'weighing options', JSON.stringify(t));
check('answer separated', t.answer === 'Final answer', JSON.stringify(t));
t = md.splitThinking('<think>a</think>X<think>b</think>Y');
check('multiple blocks joined', t.thinking === 'a\n\nb', JSON.stringify(t));
check('answer keeps both halves', t.answer === 'XY', JSON.stringify(t));
t = md.splitThinking('<reasoning>r</reasoning>done');
check('reasoning tag honoured', t.thinking === 'r', JSON.stringify(t));
t = md.splitThinking('<thinking>still going');
check('unterminated block captured', t.thinking === 'still going', JSON.stringify(t));
t = md.splitThinking('nothing special');
check('plain text untouched', t.thinking === '' && t.answer === 'nothing special', JSON.stringify(t));

console.log('\n== combined document ==');
const doc = md.render(`# Report

Some **bold** text and \`code\`.

| Col | Val |
| --- | --: |
| a | 1 |

1. step one
2. step two

> note

\`\`\`bash
echo hi
\`\`\`
`);
['<h1>', '<strong>', '<code>', '<table class="md">', '<ol>', '<blockquote>', '<figure class="code"']
  .forEach((needle) => check(`document contains ${needle}`, doc.includes(needle), doc.slice(0, 200)));

console.log('\n' + '='.repeat(52));
if (failed.length) {
  console.log(`FAILED (${failed.length}): ${failed.join(', ')}`);
  process.exit(1);
}
console.log('MARKDOWN RENDERER CONFIRMED');
