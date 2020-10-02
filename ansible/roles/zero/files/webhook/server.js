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
serialPort.on('error', function(err) {
  console.log('Error: ', err.message)
})

const app = express();
app.use(express.json());

function write(text) {
  serialPort.write(text, function(err) {
    if (err) {
      return console.log('Error on write: ', err.message)
    }
  });
}

var lastSeenPlaying = 0;
var lastSeenStopped = 0;

function playing() {
  if (lastSeenPlaying < Date.now() - 1000) {
    lastSeenPlaying = Date.now();
    console.log("PLAYING");
    write('$I5\r\n');
  } else {
    console.log("debouncing playing");
  }
}

function stopped() {
  if (lastSeenStopped < Date.now() - 1000) {
    lastSeenStopped = Date.now();
    console.log("STOPPED");
    write('$I1\r\n');
  } else {
    console.log("debouncing stopped");
  }
}

// Access the parse results as request.body
app.post('/', function(request, response){
    console.log("Webhook...");
    if (request.body && request.body.type === "transport-state") {
      if (request.body.data && request.body.data.roomName === "Office") {
        if (request.body.data.state && request.body.data.state.playbackState && request.body.data.state.playbackState === "PLAYING") {
          playing();
        } else {
          stopped();
        }
      }
    }
});

app.listen(port, () => console.log(`Example app listening at http://localhost:${port}`))
