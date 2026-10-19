// 2-space indent, single quotes, camelCase, semicolons.
import { createStore } from './store';

const DEFAULT_NAME = 'world';

export function greet(name, options = {}) {
  const label = name || DEFAULT_NAME;
  if (options.loud) {
    console.log(`HELLO ${label}`);
  } else {
    console.log('hello ' + label);
  }
  return label;
}

const items = [
  'one',
  'two',
  'three',
];

export default { greet, items, createStore };
