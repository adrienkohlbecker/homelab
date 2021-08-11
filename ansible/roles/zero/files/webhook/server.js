const express = require('express');
const MarantzDenonTelnet = require('marantz-denon-telnet');
const req = require('request');

const port = 3000;
const marantz = new MarantzDenonTelnet("10.123.30.121");

const app = express();
app.use(express.json());

var playingTelnet = false;
let wantStopped = false;

function setPlayingTelnet() {
  if (!playingTelnet) {
    if (wantStopped) {
      wantStopped = false
      playingTelnet = true
      console.log("cancelling stop")
    } else {
      playingTelnet = true;
      console.log("PLAYING LIVING ROOM");
      marantz.cmd('PWON', function (error, ret) {
        error ? console.log(error) : marantz.cmd('SICD', function (error, ret) {
          error ? console.log(error) : setTimeout(function () {
            marantz.cmd('MV35', function (error, ret) {
              console.log((error ? error : 'Sent command to turn AVR on.'));
            })
          }, 2000);
        })
      });
    }
  } else {
    console.log("debouncing playing");
  }
}

function setStoppedTelnet() {
  if (playingTelnet) {
    playingTelnet = false;
    wantStopped = true;
    console.log("Stopping in 5 seconds")
    setTimeout(function () {
      if (wantStopped) {
        wantStopped = false
        console.log("STOPPED LIVING ROOM");
        marantz.cmd('PWSTANDBY', function (error, ret) {
          console.log((error ? error : 'Sent command to turn AVR off.'));
        });
      }
    }, 5000);
  } else {
    console.log("debouncing stopped");
  }
}

// Access the parse results as request.body
app.post('/', function (request, _) {
  if (request.body && request.body.type === "transport-state") {
    console.log("Webhook..." + request.body.type + ' ' + (request.body.data.state.playbackState || '') + ' ' + (request.body.data.roomName || ' '));

    zone_uuid = request.body.data.uuid

    req.get("http://localhost:5005/zones", { json: true }, function (err, res, body) {
      if (err) { console.log(err); return }

      body.forEach(zone => {
        if (zone.uuid == zone_uuid) {

          zone.members.forEach(member => {
            if (member.roomName === "Living Room") {
              console.log('playingTelnet ' + playingTelnet + ' wantStopped ' + wantStopped)
              if (request.body.data.state && request.body.data.state.playbackState && request.body.data.state.playbackState === "PLAYING") {
                setPlayingTelnet();
              } else {
                setStoppedTelnet();
              }
            }
          })
        }
      });
    });
  }
});

app.listen(port, () => console.log(`Example app listening at http://localhost:${port}`))
