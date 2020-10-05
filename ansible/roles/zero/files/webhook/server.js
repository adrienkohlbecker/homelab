const fs = require('fs');
const process = require('process');
const express = require('express');
const SerialPort = require('serialport');
const MarantzDenonTelnet = require('marantz-denon-telnet');

const port = 3000;
const serialPath = "/dev/ttyACM0";

const marantz = new MarantzDenonTelnet("10.123.30.121");

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

var playingSerial = false;
var playingTelnet = false;

function setPlayingSerial() {
  if (!playingSerial) {
    playingSerial = true;
    console.log("PLAYING OFFICE");
    write('$I5\r\n');
  } else {
    console.log("debouncing playing");
  }
}

function setStoppedSerial() {
  if (playingSerial) {
    playingSerial = false;
    console.log("STOPPED OFFICE");
    write('$I1\r\n');
  } else {
    console.log("debouncing stopped");
  }
}

function setPlayingTelnet() {
  if (!playingTelnet) {
    playingTelnet = true;
    console.log("PLAYING LIVING ROOM");
    marantz.cmd('PWON', function(error, ret) {
      error ? console.log(error) : marantz.cmd('SICD', function (error, ret) {
        error ? console.log(error) : setTimeout(function() {
          marantz.cmd('MV45', function (error, ret) {
            console.log((error ? error : 'Sent command to turn AVR on.'));
          })
        }, 2000);
      })
    });
  } else {
    console.log("debouncing playing");
  }
}

function setStoppedTelnet() {
  if (playingTelnet) {
    playingTelnet = false;
    console.log("STOPPED LIVING ROOM");
    marantz.cmd('PWSTANDBY', function(error, ret) {
      console.log((error ? error : 'Sent command to turn AVR off.'));
    });
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
          setPlayingSerial();
        } else {
          setStoppedSerial();
        }
      } else if (request.body.data && request.body.data.roomName === "Living Room") {
        if (request.body.data.state && request.body.data.state.playbackState && request.body.data.state.playbackState === "PLAYING") {
          setPlayingTelnet();
        } else {
          setStoppedTelnet();
        }
      }
    }
});

app.listen(port, () => console.log(`Example app listening at http://localhost:${port}`))
