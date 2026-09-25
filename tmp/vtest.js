console.log('electron runtime version:', process.versions.electron || 'MISSING');
console.log('typeof require("electron"):', typeof require('electron'));
process.exit(0);
