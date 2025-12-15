const { spawn } = require('child_process');

async function testCommand(name, command, args) {
    console.log(`\n=== ${name} ===`);
    console.log(`Command: ${command} ${args.join(' ')}`);
    
    const proc = spawn(command, args, {
        cwd: process.cwd(),
        env: { ...process.env, PYTHONUNBUFFERED: '1' },
    });
    
    console.log(`PID: ${proc.pid}`);
    
    const timeout = setTimeout(() => {
        console.log('TIMEOUT - killing');
        proc.kill('SIGKILL');
    }, 15000);
    
    let stdout = '';
    proc.stdout?.on('data', (d) => { 
        stdout += d.toString(); 
        console.log(`[stdout] ${d.toString().substring(0, 300)}`);
    });
    proc.stderr?.on('data', (d) => { 
        console.log(`[stderr] ${d.toString()}`);
    });
    
    await new Promise((resolve) => {
        proc.on('exit', (code) => {
            clearTimeout(timeout);
            console.log(`Exit: code=${code}, Total stdout: ${stdout.length} bytes`);
            resolve();
        });
    });
}

async function main() {
    await testCommand('global qqcode', 'qqcode', ['--prompt', 'say hello briefly', '--output', 'vscode']);
}

main().catch(console.error);
