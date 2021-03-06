import os
import sys
import time
import shutil
import io
import email
import argparse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import smtplib
import threading

## Grab the backported enums from python3.4
from enum import Enum

import piggyphoto
import pygame
from pygame.locals import *
import easygui
import PIL
import serial
import numpy
from scipy import ndimage

PREVIEW = '/mnt/tmp/preview.jpg'
STORE_DIR = 'images'
SAVE_PREFIX = 'Booth'
STRIP_SUFFIX = 'Strip'
CAPTION = "Python Photobooth"
FROM_ADDR = 'auto-mailer@newport.net.nz'
SERIAL = '/dev/ttyACM0'
READY_WAIT = 5  # seconds for the 'Get Ready!' prompt
COUNTDOWN_WAIT = 1  # seconds between 3..2..1
SHOT_COUNT = 3
SCRIPT_DIR = './'


class BoothState(Enum):
    waiting = 1
    shooting = 2
    email = 3
    thanks = 4
    quit = 5


class ShootPhase(Enum):
    get_ready = 4
    countdown_three = 3
    countdown_two = 2
    countdown_one = 1
    shoot = 5

    def __next__(self):
        if self == ShootPhase.shoot:
            return ShootPhase.countdown_three
        elif self == ShootPhase.countdown_one:
            return ShootPhase.shoot
        return ShootPhase(self.value - 1)


class BoothView(object):
    def __init__(self, width=900, height=768, fps=5, fullscreen=True):
        """Initialize the bits"""
        pygame.init()
        pygame.display.set_caption(CAPTION)
        dir = os.path.dirname(PREVIEW)
        if not os.access(dir, os.W_OK):
            print 'Directory {} is not writable. Ensure it exists and is writable.\n'.format(dir)
            print 'Try `mount -t tmpfs -o size=100m tmpfs {}` to create a ramdisk in that location\n'.format(dir)
            pygame.quit()
            sys.exit()
        self.width = width
        self.height = height
        self.screen = None
        self.fullscreen = fullscreen
        if fullscreen:
            self.screen = pygame.display.set_mode((0,0), pygame.FULLSCREEN | pygame.DOUBLEBUF)
            (self.width, self.height) = self.screen.get_size()
        else:
            self.screen = pygame.display.set_mode((self.width, self.height), pygame.DOUBLEBUF)
        self.background = pygame.Surface(self.screen.get_size()).convert()
        self.clock = pygame.time.Clock()
        self.base_fps = fps
        self.fps = fps
        self.countdown = READY_WAIT
        self.small_font = pygame.font.SysFont('Arial', 20, bold=True)
        self.large_font = pygame.font.SysFont('Arial', 40, bold=True)
        self.huge_font = pygame.font.SysFont('Arial', 150, bold=True)

        self.camera = piggyphoto.camera()
        self.camera.leave_locked()
        self.camera.capture_preview(PREVIEW)

        self.state = BoothState.waiting
        self.shoot_phase = ShootPhase.get_ready
        self.phase_start = time.time()
        self.shots_left = SHOT_COUNT
        self.shot_counter = 0
        self.session_counter = 1
        self.images = []
        self.pid = os.getpid()

    def run(self):
        running = True
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        running = False
                    elif event.key == pygame.K_RETURN and self.state == BoothState.waiting:
                        self.switch_state(BoothState.shooting)
                elif event.type == pygame.USEREVENT:
                    if event.action == 'button_pressed' and self.state == BoothState.waiting:
                        print('Actioning event from serial')
                        self.switch_state(BoothState.shooting)
            if not running:
                break
            if self.state == BoothState.waiting:
                self.wait_state()
            elif self.state == BoothState.shooting:
                self.shoot_state()
            elif self.state == BoothState.email:
                self.collect_email()
            elif self.state == BoothState.thanks:
                # Just quit for now
                self.switch_state(BoothState.waiting)

            self.clock.tick(self.fps)
            pygame.display.set_caption("{} FPS: {:6.3}".format(CAPTION, self.clock.get_fps()))
            pygame.display.flip()
        print('Exiting main loop')
        pygame.quit()

    def wait_state(self):
        # Maybe add some fancy image processing?
        self.update_image(blur=True)
        self.draw_centered_text('PRESS THE BIG RED BUTTON TO START', self.large_font, outline=True)

    def shoot_state(self):
        self.update_image()
        frame_time = time.time()
        if frame_time > self.phase_start + self.countdown:
            self.phase_start = time.time()
            if self.shoot_phase != ShootPhase.shoot:
                self.countdown = COUNTDOWN_WAIT
                self.shoot_phase = next(self.shoot_phase)
            else:
                if self.shots_left:
                    filename = os.path.join(STORE_DIR, '{0}{1}-{2:04d}{3:02d}.jpg'.format(SAVE_PREFIX, self.pid,
                                                                                          self.session_counter,
                                                                                          self.shot_counter))
                    self.camera.capture_image(filename)
                    self.images.append(filename)
                    self.shot_counter += 1
                    self.shots_left -= 1
                    self.shoot_phase = next(self.shoot_phase)
                    # Add a double time to allow for the shot time
                    self.countdown = COUNTDOWN_WAIT * 2
                    if self.shots_left == 0:
                        self.switch_state(BoothState.email)
                        return
        if self.shoot_phase == ShootPhase.get_ready:
            self.draw_centered_text('Get Ready!', self.huge_font, outline=True)
        elif self.shoot_phase != ShootPhase.shoot:
            self.draw_centered_text(str(self.shoot_phase.value), self.huge_font, outline=True)

    def collect_email(self):
        start = time.time()
        strip_file = self.generate_strip()
        finish = time.time()
        print(('Strip generation started {0}, finished {1}, elapsed {2}. Output {3}'.format(start, finish, finish - start, strip_file)))
        if self.fullscreen:
            pygame.display.toggle_fullscreen()
        email = easygui.enterbox(
            "Enter your email address if you'd like a copy sent to you:",
            "Enter your email",
            ""
        )
        print(email)
        send_email = True
        if email is None or email.endswith('example.com'):
            send_email = False
        elif email in ('null@catalyst.net.nz', ''):
            send_email = False

        if send_email:
            email_thread = threading.Thread(target=BoothView.send_strip, args=[email, strip_file])
            email_thread.start()

        if self.fullscreen:
            pygame.display.toggle_fullscreen()
        self.switch_state(BoothState.thanks)

    def update_image(self, source=None, blur=False):
        if source is None:
            camera_file = self.camera.capture_preview()
            camera_file.save(PREVIEW)
            camera_file.__dealoc__(PREVIEW)

        picture = pygame.image.load(PREVIEW)
        picture = pygame.transform.rotate(picture, -90)
        (width, height) = picture.get_size()
        new_height = self.width*(float(height)/width)
        #new_height = 800*(float(height)/width)
        picture = pygame.transform.scale(picture, (self.width, int(new_height)))
        #picture = pygame.transform.scale(picture, (800, int(new_height)))
        # Smoothscale looks better, but is a ~30% speed hit
        # picture = pygame.transform.smoothscale(picture, (int(new_width), self.height))
        tb_crop = (new_height - self.height)/2.0
        if blur:
            surface_array = pygame.surfarray.pixels3d(picture)
            # Do the resize simultaneously with adding the shade
            if tb_crop > 0:
                surface_array = surface_array[tb_crop:-tb_crop] / 2  # Gives appearance of dark overlay, ~30% speed hit
            else:
                surface_array = surface_array / 2
            tb_crop = 0
            # sigma = 3
            # Nice blur effect, but ~75% speed hit
            # surface_array = ndimage.filters.gaussian_filter(
            #     surface_array,
            #     sigma=(sigma, sigma, 0),
            #     order=0,
            #     mode='reflect'
            # )
            picture = pygame.surfarray.make_surface(surface_array)
        #self.screen.blit(picture, (0, -tb_crop))
        self.screen.blit(picture, (0, -tb_crop))

    def generate_strip(self):
        canvas = PIL.Image.open(os.path.join(SCRIPT_DIR, 'photobooth_template_portrait.jpg'))
        i = 0
        piece_dims = (833, 533)
        piece_dims = (533, 833)
        for pos in [(60,60), (607,60), (60,907)]:
            img = PIL.Image.open(self.images[i])
            img = img.rotate(-90)
            # Snip the top and bottom strips so it's the right proportion
            (dims, crop) = get_resize_transform(img.size, piece_dims)
            img = img.resize(dims, resample=PIL.Image.ANTIALIAS)
            img = img.crop((crop[0]/2, crop[1]/2, piece_dims[0]+crop[0]/2,  piece_dims[1]+crop[1]/2))
            canvas.paste(img, box=pos)
            i += 1
        strip_file = os.path.join(STORE_DIR, '{0}{1}-{2:04d}-{3}.jpg'.format(SAVE_PREFIX, self.pid, self.session_counter, STRIP_SUFFIX))
        canvas.save(strip_file)
        return strip_file

    @staticmethod
    def send_strip(email_addr, filepath):
        start = time.time()
        msg = MIMEMultipart()
        msg['From'] = FROM_ADDR
        msg['To'] = email_addr
        msg['Subject'] = 'Your Photobooth Strip!'
        body = 'Thanks for coming along to our party!\nYour photobooth strip is attached'
        msg.attach(MIMEText(body, 'plain'))
        img = open(filepath, 'rb')
        mime_image = MIMEImage(img.read())
        mime_image.add_header('Content-Disposition', 'attachment', filename=os.path.split(filepath)[-1])
        msg.attach(mime_image)

        server = smtplib.SMTP('mail.newport.net.nz')
        server.ehlo('Python Photobooth')
        server.starttls()
        server.ehlo()
        server.login(FROM_ADDR, 'aI6Y&i&ACTv9R#RFMg3m')
        server.sendmail(FROM_ADDR, email_addr, msg.as_string())
        finish = time.time()
        print(('Email sending to {4} started {0}, finished {1}, elapsed {2}. Output {3}'.format(start, finish, finish - start, filepath, email_addr)))

    def draw_centered_text(self, text, font=None, color=(255, 255, 255), outline=False):
        """Center text in window"""
        if font == None:
            font = self.small_font
        fw, fh = font.size(text)
        if outline:
            textobj = font.render(text, True, (0, 0, 0))
            for xoffset in (-1, 1):
                for yoffset in (-1, 1):
                    textpos = textobj.get_rect(centerx=self.background.get_width() / 2 + xoffset,
                                               centery=self.background.get_height() / 2 + yoffset)
                    self.screen.blit(textobj, textpos)
        textobj = font.render(text, True, color)
        textpos = textobj.get_rect(centerx=self.background.get_width() / 2, centery=self.background.get_height() / 2)
        self.screen.blit(textobj, textpos)

    def switch_state(self, target):
        if target == BoothState.shooting:
            # Transition IN to shooting
            self.countdown = READY_WAIT
            self.fps = 60  # This is a placeholder for infinity in this case...
            self.shoot_phase = ShootPhase.get_ready
            self.shots_left = SHOT_COUNT
            self.images = []
            self.phase_start = time.time()
        elif target == BoothState.waiting:
            self.fps = self.base_fps
        if self.state == BoothState.thanks:
            # When transitioning OUT of thanks
            self.shot_counter = 0
            self.session_counter += 1
        self.state = target


def serial_listener():
    try:
        s = serial.Serial(SERIAL, 9600)
    except serial.SerialException as e:
        print(e)
        return
    print('Serial listener attached')
    while True:
        command = s.readline().rstrip()
        if command == b'd':
            pygame.event.post(pygame.event.Event(pygame.USEREVENT, action='button_pressed'))
        elif command == b'r':
            print('Ready signal received from Arduino')

def get_resize_transform(current, desired):
    """Takes two dimension tuples in (w, h) form, and returns the dimension to scale to, and the number of pixels in each dimension to crop"""
    if len(current) != 2 or len(desired) != 2:
        raise ValueError('Current and desired must both contain exactly two dimensions')
    w_ratio = float(desired[0])/current[0]
    h_ratio = float(desired[1])/current[1]
    scale_factor = max(w_ratio, h_ratio)
    dims = (int(current[0] * scale_factor), int(current[1] * scale_factor))
    w_crop = dims[0] - desired[0]
    h_crop = dims[1] - desired[1]
    crop = (int(w_crop), int(h_crop))
    return (dims, crop)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='A fun photobooth')
    parser.add_argument('--serial', help='the serial port to listen for a button', default='/dev/ttyACM0', nargs='?', type=str)
    #parser.add_argument('camera', help='the gphoto device to use', default="/dev/ttyACM0")

    args = parser.parse_args()
    SERIAL = args.serial

    SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

    if not os.path.exists(STORE_DIR):
        os.mkdir(STORE_DIR)
    listener = threading.Thread(target=serial_listener)
    listener.daemon = True
    listener.start()
    BoothView(width=1050, height=1680, fullscreen=False).run()

def quit_pressed():
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            return True
    # Don't quit... someone might be messing around
    return False
