const { app } = require('electron');
console.log('electron version:', process.versions.electron || 'MISSING');
console.log('app defined:', typeof app);
process.exit(0);
