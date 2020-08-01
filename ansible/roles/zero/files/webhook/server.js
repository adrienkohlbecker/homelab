const fs = require('fs');
const process = require('process');
const express = require('express');
const SerialPort = require('serialport')

const port = 3000;
const serialPath = "/dev/ttyACM0";

try {
  fs.accessSync(serialPath, fs.constants.R_OK | fs.constants.W_OK);
} catch (err) {
  console.error(`No access to ${serialPath}, are you root?`);
  process.exit(1);
}

const serialPort = new SerialPort(serialPath, { baudRate: 115200 })

const app = express();
app.use(express.json());

// Access the parse results as request.body
app.post('/', function(request, response){
    if (request.body && request.body.type === "transport-state") {
      if (request.body.data && request.body.data.roomName === "Office") {
        if (request.body.data.state && request.body.data.state.playbackState && request.body.data.state.playbackState === "PLAYING") {
          console.log("PLAYING");
          serialPort.write('$I5\r\n');
        } else {
          console.log("STOPPED");
          serialPort.write('$I1\r\n');
        }
      }
    }
});

app.listen(port, () => console.log(`Example app listening at http://localhost:${port}`))
