package com.example.controller;

import com.example.service.UserService;
import com.example.model.User;

public class UserController {
    private UserService userService;

    public UserController(UserService userService) {
        this.userService = userService;
    }

    public User handleRequest(String userId) {
        return userService.getUser(userId);
    }

    public User createUser(String name, String email) {
        return userService.createUser(name, email);
    }
}
