const e = require('electron');
console.log('typeof:', typeof e);
console.log('value:', typeof e === 'string' ? e.slice(0,60) : Object.keys(e).slice(0,6));
